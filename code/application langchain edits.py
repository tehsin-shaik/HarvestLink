import hashlib
import hmac
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from PIL import Image, UnidentifiedImageError
import pandas as pd
import streamlit as st
from decimal import Decimal
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

load_dotenv(override=True)

st.set_page_config(page_title="HarvestLink | Local Farm Market", page_icon="🌱", layout="wide")


def loadAuthStyles():
    # Keep appearance separate from authentication and database logic.
    cssPath = Path(__file__).parent / "style.css"
    if cssPath.is_file():
        st.markdown(f"<style>{cssPath.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


loadAuthStyles()

IMAGE_FOLDER = Path(__file__).parent / "images"
IMAGE_FOLDER.mkdir(exist_ok=True)

roles = {"farmer", "user", "admin"}
requestStatus = {"pending", "partiallyFulfilled", "fullyFulfilled", "canceled"}
allocStatus = {"reserved", "partiallyDelivered", "delivered", "canceled"}
movementTypes = {"added", "reserved", "canceled", "delivered", "loss", "correction_added", "correction_removed"}


# -----------------------------------------------------------------------------
# Database helpers
# -----------------------------------------------------------------------------

def buildDbUri() -> str:
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER", "root")
    password = quote_plus(os.getenv("DB_PASSWORD", ""))
    db_name = os.getenv("DB_NAME", "DBProjGroup3")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db_name}"


def getModelName() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


@st.cache_resource
def getEngine():
    return create_engine(buildDbUri(), pool_pre_ping=True)


def getConnection():
    return getEngine().raw_connection()


def friendlyDatabaseError(error):
    original_error = getattr(error, "orig", error)
    error_number = getattr(original_error, "errno", None)

    if error_number is None and original_error.args:
        error_number = original_error.args[0]

    if error_number == 1062:
        return "That value already exists. Please enter a different value."
    if error_number == 1452:
        return "A related record could not be found. Please refresh and try again."
    if error_number in (1048, 1364):
        return "A required value is missing. Please complete all required fields."
    if error_number in (1264, 3819, 4025):
        return "One of the entered values is outside the allowed range."
    if error_number == 1045:
        return "The database username or password is incorrect."
    if error_number in (2003, 2005):
        return "Could not connect to MySQL. Check that MySQL is running."

    return "The operation could not be completed. Please check the entered values."


def selectData(query, values=(), show_error=True):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(query, values)
        rows = cursor.fetchall()
        columns = [column[0] for column in cursor.description]
        return pd.DataFrame(rows, columns=columns)
    except Exception as error:
        print("Database read error:", error)
        if show_error:
            st.error(friendlyDatabaseError(error))
        return pd.DataFrame()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def changeData(query, values=()):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(query, values)
        connection.commit()
        return True
    except Exception as error:
        if connection:
            connection.rollback()
        print("Database change error:", error)
        st.error(friendlyDatabaseError(error))
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


passwordHasher = PasswordHasher()  # Argon2id includes a new random salt each time.


def hashPassword(password):
    # New accounts get a salted password hash, never a plain SHA-256 hash.
    return passwordHasher.hash(password)


def verifyPassword(password, storedHash):
    # Old SHA-256 accounts can log in once, then we upgrade their hash.
    storedHash = str(storedHash)
    if re.fullmatch(r"[0-9a-fA-F]{64}", storedHash):
        oldHash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(oldHash, storedHash.lower()), True
    if not storedHash.startswith("$argon2id$"):
        return False, False
    try:
        return passwordHasher.verify(storedHash, password), False
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False, False


def isValidEmail(email):
    return re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email) is not None


def isValidName(name):
    return re.fullmatch(r"[A-Za-z][A-Za-z '-]{1,49}", name) is not None


def logActivity(userID, actionType, details=None):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(
            """INSERT INTO UserActivityLog
               (userID, actionType, actionDate, details)
               VALUES (%s, %s, %s, %s)""",
            (userID, actionType, datetime.now(), details)
        )
        connection.commit()
    except Exception as error:
        if connection:
            connection.rollback()
        print("Activity log error:", error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def denyAccess(currentUser, details):
    userID = int(currentUser["userID"])
    logActivity(userID, "access_denied", details)
    st.warning("Access denied. You do not have permission to perform this action.")


def getActiveUnits():
    # Get the units the admin has allowed instead of keeping a Python list.
    units = selectData(
        "SELECT unitName FROM Units WHERE isActive = 1 ORDER BY unitName"
    )
    if units.empty:
        return []
    return units["unitName"].astype(str).tolist()


def isValidUnit(unit):
    return str(unit).strip().lower() in getActiveUnits()


def addReferenceUnit(unitName, adminID):
    # Only add a unit if it is not already in the database.
    unitName = str(unitName).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", unitName):
        return False, "Use 1-20 letters, numbers or underscores, starting with a letter."
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute("SELECT role FROM Users WHERE userID = %s", (adminID,))
        adminRow = cursor.fetchone()
        if adminRow is None or str(adminRow[0]).lower() != "admin":
            connection.rollback()
            return False, "Only administrators can manage units."
        cursor.execute(
            "SELECT unitName FROM Units WHERE unitName = %s", (unitName,)
        )
        if cursor.fetchone() is not None:
            connection.rollback()
            return False, "That unit already exists. You can reactivate it from its row."
        cursor.execute(
            "INSERT INTO Units (unitName, isActive) VALUES (%s, 1)", (unitName,)
        )
        connection.commit()
        logActivity(adminID, "unit_added", f"Added unit {unitName}")
        return True, "Unit added successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Unit creation error:", repr(error))
        return False, friendlyDatabaseError(error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def setReferenceUnitActive(unitName, isActive, adminID):
    # Keep old unit values in transactions even when a unit is turned off.
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute("SELECT role FROM Users WHERE userID = %s", (adminID,))
        adminRow = cursor.fetchone()
        if adminRow is None or str(adminRow[0]).lower() != "admin":
            connection.rollback()
            return False, "Only administrators can manage units."
        cursor.execute(
            "SELECT isActive FROM Units WHERE unitName = %s FOR UPDATE", (unitName,)
        )
        row = cursor.fetchone()
        if row is None:
            connection.rollback()
            return False, "Unit not found. Please refresh."
        if bool(row[0]) == bool(isActive):
            connection.rollback()
            return False, "This unit already has that status."
        cursor.execute(
            "UPDATE Units SET isActive = %s WHERE unitName = %s",
            (1 if isActive else 0, unitName)
        )
        connection.commit()
        logActivity(adminID, "unit_status_changed",
                    f"Set unit {unitName} to {'active' if isActive else 'inactive'}")
        return True, "Unit status updated successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Unit status error:", repr(error))
        return False, friendlyDatabaseError(error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def changeReferenceCrop(cropID, cropName, isActive, adminID):
    # Admin edits keep crop IDs and historical transactions unchanged.
    name = str(cropName).strip()
    if not name or len(name) > 100:
        return False, "Enter a crop name of 1-100 characters."
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute("SELECT role FROM Users WHERE userID = %s", (adminID,))
        row = cursor.fetchone()
        if row is None or str(row[0]).lower() != "admin":
            return False, "Only administrators can manage crops."
        cursor.execute("SELECT cropName, isActive FROM Crops WHERE cropID = %s FOR UPDATE", (cropID,))
        original = cursor.fetchone()
        if original is None:
            return False, "Crop not found. Please refresh."
        cursor.execute(
            "SELECT cropID FROM Crops WHERE cropName = %s AND cropID <> %s",
            (name, cropID)
        )
        if cursor.fetchone() is not None:
            return False, "A different crop already has that name."
        active = 1 if isActive else 0
        if str(original[0]) == name and int(original[1]) == active:
            return False, "No changes to save."
        cursor.execute(
            "UPDATE Crops SET cropName = %s, isActive = %s WHERE cropID = %s",
            (name, active, cropID)
        )
        connection.commit()
        logActivity(adminID, "admin_crop_updated", f"Crop {cropID}: {original[0]} -> {name}; active={active}")
        return True, "Crop updated successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Crop edit error:", repr(error))
        return False, friendlyDatabaseError(error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def changeReferenceMarket(marketID, marketName, location, isActive, adminID):
    # Updating/deactivating a market does not remove past requests or receipts.
    name = str(marketName).strip()
    place = str(location).strip()
    if not name or len(name) > 100 or not place or len(place) > 255:
        return False, "Enter a market name (1-100) and location (1-255)."
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute("SELECT role FROM Users WHERE userID = %s", (adminID,))
        row = cursor.fetchone()
        if row is None or str(row[0]).lower() != "admin":
            return False, "Only administrators can manage markets."
        cursor.execute(
            "SELECT marketName, location, isActive FROM Markets WHERE marketID = %s FOR UPDATE",
            (marketID,)
        )
        original = cursor.fetchone()
        if original is None:
            return False, "Market not found. Please refresh."
        active = 1 if isActive else 0
        if str(original[0]) == name and str(original[1]) == place and int(original[2]) == active:
            return False, "No changes to save."
        cursor.execute(
            "UPDATE Markets SET marketName = %s, location = %s, isActive = %s WHERE marketID = %s",
            (name, place, active, marketID)
        )
        connection.commit()
        logActivity(adminID, "admin_market_updated", f"Market {marketID}: {original[0]} -> {name}; active={active}")
        return True, "Market updated successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Market edit error:", repr(error))
        return False, friendlyDatabaseError(error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def signUpUser(
    fname,
    lname,
    email,
    password,
    role,
    farmName="",
    farmLocation=""
):
    fname = fname.strip()
    lname = lname.strip()
    email = email.strip().lower()
    farmName = farmName.strip()
    farmLocation = farmLocation.strip()

    if not isValidName(fname):
        return False, "Enter a valid first name."

    if not isValidName(lname):
        return False, "Enter a valid last name."

    if not isValidEmail(email):
        return False, "Enter a valid email address."

    if len(password) < 6:
        return False, (
            "Password must contain at least 6 characters."
        )

    if role not in {"farmer", "user"}:
        return False, "Choose a valid account role."

    if (
        role == "farmer"
        and (not farmName or not farmLocation)
    ):
        return False, (
            "Enter the farm name and location."
        )

    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT userID
               FROM Users
               WHERE email = %s""",
            (email,)
        )

        if cursor.fetchone() is not None:
            return False, (
                "An account already exists with that email."
            )

        cursor.execute(
            """INSERT INTO Users
               (fname, lname, role, email, passwordHash)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                fname,
                lname,
                role,
                email,
                hashPassword(password)
            )
        )

        userID = cursor.lastrowid

        if role == "farmer":
            cursor.execute(
                """INSERT INTO Farms
                   (farmName, location)
                   VALUES (%s, %s)""",
                (
                    farmName,
                    farmLocation
                )
            )

            farmID = cursor.lastrowid

            cursor.execute(
                """INSERT INTO OwnedBy
                   (userID, farmID)
                   VALUES (%s, %s)""",
                (
                    userID,
                    farmID
                )
            )

        connection.commit()

        return True, (
            "Account created. You can now sign in."
        )

    except Exception as error:
        if connection:
            connection.rollback()

        print("Signup error:", error)

        return False, friendlyDatabaseError(error)

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


def createFarmForUser(userID, farmName, farmLocation):
    connection = None
    cursor = None

    try:
        farmName = farmName.strip()
        farmLocation = farmLocation.strip()

        if not farmName or not farmLocation:
            return False, "Enter the farm name and location."

        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO Farms (farmName, location) VALUES (%s, %s)",
            (farmName, farmLocation)
        )
        farmID = cursor.lastrowid
        cursor.execute(
            "INSERT INTO OwnedBy (userID, farmID) VALUES (%s, %s)",
            (userID, farmID)
        )
        connection.commit()
        logActivity(userID, "farm_created", f"Created farm {farmID}")
        return True, "Farm created successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Farm creation error:", error)
        return False, friendlyDatabaseError(error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def refreshSignedInUser(currentUser):
    # Re-read the account on every run, so a role change made by an
    # administrator applies on the user's next action, not their next login.
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT userID, fname, lname, role, email FROM Users WHERE userID = %s",
            (int(currentUser["userID"]),)
        )
        user = cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    if user is None:
        return None
    return {
        "userID": user[0],
        "fname": user[1],
        "lname": user[2],
        "fullname": f"{user[1]} {user[2]}",
        "role": str(user[3]).strip().lower(),
        "email": user[4]
    }


def signInUser(email, password):
    email = email.strip().lower()

    if not isValidEmail(email):
        return None, "Enter a valid email address."

    if not password:
        return None, "Enter your password."

    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        query = "SELECT userID, fname, lname, role, email, passwordHash FROM Users WHERE email = %s"
        cursor.execute(query, (email,))
        user = cursor.fetchone()

        if user is None:
            return None, "The email or password is incorrect."

        validPassword, oldHash = verifyPassword(password, user[5])
        if not validPassword:
            return None, "The email or password is incorrect."

        if oldHash or passwordHasher.check_needs_rehash(str(user[5])):
            # Upgrade old hashes only after the correct password was entered.
            cursor.execute(
                "UPDATE Users SET passwordHash = %s "
                "WHERE userID = %s AND passwordHash = %s",
                (hashPassword(password), user[0], user[5])
            )
            connection.commit()

        signedInUser = {
            "userID": user[0],
            "fname": user[1],
            "lname": user[2],
            "fullname": f"{user[1]} {user[2]}",
            "role": str(user[3]).strip().lower(),
            "email": user[4]
        }

        logActivity(user[0], "login", "User signed in")
        return signedInUser, "Signed in successfully."

    except Exception as error:
        print("LOGIN ERROR:", error)
        return None, friendlyDatabaseError(error)

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


LEAF_LOGO = (
    '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">'
    '<path fill="currentColor" d="M20.5 3.5c-8.6.2-14 3.6-15.6 9.3-.6 2.1-.4 4 .4 5.6'
    'L3 20.7l1.3 1.3 2.3-2.3c1.6.8 3.5 1 5.6.4 5.7-1.6 9.1-7 9.3-15.6l.1-1.1-1.1.1z'
    'M8.3 17.1c1.2-3.3 3.7-6.1 7.3-8.1-3.1 2.4-5.2 5.1-6.2 8.4l-1.1-.3z"/></svg>'
)

# Seeded harvest photos used to decorate the sign-in showcase.
SHOWCASE_PRODUCE = (
    ("lot_2_1_strawberries.png", "Strawberries"),
    ("lot_5_1_carrots.png", "Carrots"),
    ("lot_8_1_apples.png", "Apples"),
    ("lot_4_1_papaya.png", "Papaya"),
)


@st.cache_data(show_spinner=False)
def showcaseImageData(imageName):
    import base64
    from io import BytesIO
    from PIL import ImageOps

    imagePath = resolveHarvestImagePath(imageName)
    if imagePath is None:
        return None
    try:
        with Image.open(imagePath) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, (360, 360))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=82)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as error:
        print(f"Showcase image failed ({imageName!r}): {error!r}")
        return None


def showcaseTilesHtml():
    tiles = []
    for imageName, label in SHOWCASE_PRODUCE:
        data = showcaseImageData(imageName)
        if data:
            tiles.append(
                f'<figure class="hl-tile"><img src="data:image/jpeg;base64,{data}" alt="{label}">'
                f'<figcaption>{label}</figcaption></figure>'
            )
    if not tiles:
        return ""
    return f'<div class="hl-tiles">{"".join(tiles)}</div>'


def showStoreBar(rightHtml=""):
    st.markdown(
        f"""<div class="hl-storebar">
            <div class="hl-logo"><span class="hl-logo-mark">{LEAF_LOGO}</span><span>Harvest<b>Link</b></span></div>
            <div class="hl-storebar-right">{rightHtml}</div>
        </div>""",
        unsafe_allow_html=True
    )


def showAuthentication():
    # This marker applies the storefront theme only on the unauthenticated screen.
    st.markdown('<div class="hl-auth-marker"></div>', unsafe_allow_html=True)
    showStoreBar(
        '<span class="hl-chip">Fresh local produce</span>'
        '<span class="hl-chip">Direct from UAE farms</span>'
    )

    if st.session_state.pop("signedOutMessage", None):
        st.toast("You have signed out. See you soon!", icon="👋")
    if st.session_state.pop("accountRemovedMessage", None):
        st.warning("Your account is no longer available, so you have been signed out.")

    left, right = st.columns([1.15, 1], gap="large", vertical_alignment="center")

    with left:
        st.markdown(
            f"""<div class="hl-hero">
                <span class="hl-eyebrow">FARM-TO-MARKET MARKETPLACE</span>
                <h1>Fresh from local farms, <em>straight to your market.</em></h1>
                <p>Browse harvest-fresh produce from nearby farms, request what your
                market needs, and track every delivery in one place.</p>
                {showcaseTilesHtml()}
                <div class="hl-trust">
                    <div><b>Harvest-fresh</b><span>Live stock from real harvest lots</span></div>
                    <div><b>Reserve &amp; deliver</b><span>Requests, allocations and receipts</span></div>
                    <div><b>Secure accounts</b><span>Role-based access for every user</span></div>
                </div>
            </div>""",
            unsafe_allow_html=True
        )

    with right:
        with st.container(key="hl_auth_card"):
            st.markdown(
                """<div class="hl-auth-heading">
                    <span class="hl-eyebrow">WELCOME TO HARVESTLINK</span>
                    <h2>Sign in to shop local</h2>
                    <p>New here? Create a farmer or market buyer account in a minute.</p>
                </div>""",
                unsafe_allow_html=True
            )
            signInTab, signUpTab = st.tabs(["Sign in", "Create account"])

            with signInTab:
                with st.form("sign_in_form", border=False):
                    signInEmail = st.text_input(
                        "Email address", placeholder="you@example.com", key="sign_in_email"
                    )
                    signInPassword = st.text_input(
                        "Password", type="password", placeholder="Enter your password",
                        key="sign_in_password"
                    )
                    signInSubmitted = st.form_submit_button(
                        "Sign in", use_container_width=True, type="primary"
                    )

                if signInSubmitted:
                    user, message = signInUser(signInEmail, signInPassword)
                    if user:
                        st.session_state.currentUser = user
                        st.session_state.sessionStartedAt = datetime.now()
                        st.rerun()
                    else:
                        st.error(message)

            with signUpTab:
                roleLabel = st.radio(
                    "I am joining as a", ["Market buyer", "Farmer"], key="sign_up_role",
                    horizontal=True,
                    help="Farmers manage harvests; market buyers search and request produce."
                )
                with st.form("sign_up_form", border=False):
                    nameLeft, nameRight = st.columns(2)
                    with nameLeft:
                        firstName = st.text_input("First name")
                    with nameRight:
                        lastName = st.text_input("Last name")
                    signUpEmail = st.text_input(
                        "Email address", placeholder="you@example.com", key="sign_up_email"
                    )
                    passLeft, passRight = st.columns(2)
                    with passLeft:
                        signUpPassword = st.text_input(
                            "Password", type="password", key="sign_up_password"
                        )
                    with passRight:
                        confirmPassword = st.text_input(
                            "Confirm password", type="password"
                        )
                    farmName = ""
                    farmLocation = ""
                    if roleLabel == "Farmer":
                        farmLeft, farmRight = st.columns(2)
                        with farmLeft:
                            farmName = st.text_input("First farm name")
                        with farmRight:
                            farmLocation = st.text_input("Farm location or emirate")

                    signUpSubmitted = st.form_submit_button(
                        "Create my account", use_container_width=True, type="primary"
                    )

                if signUpSubmitted:
                    if signUpPassword != confirmPassword:
                        st.error("The passwords do not match.")
                    else:
                        role = "farmer" if roleLabel == "Farmer" else "user"
                        created, message = signUpUser(
                            firstName, lastName, signUpEmail, signUpPassword,
                            role, farmName, farmLocation
                        )
                        if created:
                            st.success(message)
                        else:
                            st.error(message)

def makeOptions(dataframe, idColumn, labelColumns):
    options = {}
    for _, row in dataframe.iterrows():
        label = " | ".join(str(row[column]) for column in labelColumns)
        options[label] = row[idColumn]
    return options

def updateRequestStatus(
    cursor,
    requestID,
    newStatus,
    changedBy,
    reason=None
):
    cursor.execute(
        """SELECT reqStatus
           FROM MarketRequests
           WHERE requestID = %s""",
        (requestID,)
    )

    result = cursor.fetchone()

    if result is None:
        raise ValueError("The market request could not be found.")

    oldStatus = str(result[0])

    if oldStatus == newStatus:
        return

    cursor.execute(
        """UPDATE MarketRequests
           SET reqStatus = %s
           WHERE requestID = %s""",
        (
            newStatus,
            requestID
        )
    )

    cursor.execute(
        """INSERT INTO RequestStatusHistory
           (requestID, oldStatus, newStatus,
            changedAt, changedBy, reason)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            requestID,
            oldStatus,
            newStatus,
            datetime.now(),
            changedBy,
            reason
        )
    )
def getLockedLotStock(cursor, lotID):
    # Count the latest stock using the same transaction, not another connection.
    cursor.execute(
        """SELECT movementType, quantity
           FROM StockMovements
           WHERE lotID = %s""", (lotID,)
    )
    onHand = Decimal("0")
    reserved = Decimal("0")
    for kind, amount in cursor.fetchall():
        amount = Decimal(str(amount))
        if kind in ("added", "correction_added"):
            onHand += amount
        elif kind in ("loss", "correction_removed"):
            onHand -= amount
        elif kind == "reserved":
            reserved += amount
        elif kind == "delivered":
            onHand -= amount
            reserved -= amount
        elif kind == "canceled":
            reserved -= amount
    if onHand < 0 or reserved < 0 or onHand < reserved:
        raise ValueError("Inconsistent stock movements for this lot")
    return onHand, reserved, onHand - reserved


def getLockedRequestProgress(cursor, requestID):
    # Calculate the request using the records in our current transaction.
    cursor.execute(
        """SELECT quantityRequested, reqStatus
           FROM MarketRequests WHERE requestID = %s""", (requestID,)
    )
    request = cursor.fetchone()
    if request is None:
        raise ValueError("Request was not found")
    requested = Decimal(str(request[0]))
    status = str(request[1])
    cursor.execute(
        """SELECT COALESCE(SUM(receivedQuantity), 0)
           FROM DeliveryReceipts WHERE requestID = %s""", (requestID,)
    )
    received = Decimal(str(cursor.fetchone()[0]))
    cursor.execute(
        """SELECT sm.movementType, sm.quantity
           FROM Allocations a, StockMovements sm
           WHERE a.allocationID = sm.allocationID AND a.requestID = %s""",
        (requestID,)
    )
    reserved = Decimal("0")
    for kind, amount in cursor.fetchall():
        amount = Decimal(str(amount))
        if kind == "reserved":
            reserved += amount
        elif kind in ("delivered", "canceled"):
            reserved -= amount
    if reserved < 0 or received < 0 or received + reserved > requested:
        raise ValueError("Inconsistent request quantities")
    return requested, received, reserved, status


def getReservedForAllocation(cursor, allocationID):
    cursor.execute(
        """SELECT movementType, quantity FROM StockMovements
           WHERE allocationID = %s""", (allocationID,)
    )
    reserved = Decimal("0")
    delivered = Decimal("0")
    for kind, amount in cursor.fetchall():
        amount = Decimal(str(amount))
        if kind == "reserved":
            reserved += amount
        elif kind == "canceled":
            reserved -= amount
        elif kind == "delivered":
            reserved -= amount
            delivered += amount
    if reserved < 0:
        raise ValueError("Invalid allocation movements")
    return reserved, delivered


def requestStatusFromProgress(requested, received, reserved, oldStatus):
    if oldStatus == "canceled":
        return "canceled"
    if received >= requested:
        return "fullyFulfilled"
    if received > 0 or reserved > 0:
        return "partiallyFulfilled"
    return "pending"


def recordStockAdjustment(lotID, movementType, quantity, unit, reason, userID):
    connection = None
    cursor = None
    try:
        quantity = Decimal(str(quantity))
        if not quantity.is_finite() or quantity <= 0 or movementType not in {
            "loss", "correction_added", "correction_removed"
        }:
            return False, "Choose a valid movement and positive quantity."
        if movementType.startswith("correction") and not str(reason or "").strip():
            return False, "Enter the reason for the approved correction."
        connection = getConnection()
        cursor = connection.cursor()
        # Read the latest committed movements after waiting for a lock.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        # Lock the lot before checking whether stock can be removed.
        cursor.execute(
            """SELECT unit, status FROM HarvestLots
               WHERE lotID = %s FOR UPDATE""", (lotID,)
        )
        lot = cursor.fetchone()
        if lot is None:
            connection.rollback()
            return False, "This harvest lot is unavailable."
        cursor.execute(
            """SELECT cc.farmID FROM HarvestLots hl, CropCycle cc, OwnedBy o
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = o.farmID
               AND hl.lotID = %s AND o.userID = %s""", (lotID, userID),
        )
        if cursor.fetchone() is None:
            connection.rollback()
            return False, "This lot does not belong to your farm."
        if str(lot[0]) != str(unit):
            connection.rollback()
            return False, "The stock unit has changed. Please refresh."
        _, _, available = getLockedLotStock(cursor, lotID)
        if movementType != "correction_added" and quantity > available:
            connection.rollback()
            return False, f"Only {available:,.2f} {unit} is currently available."
        cursor.execute(
            """INSERT INTO StockMovements
               (lotID, allocationID, movementType, quantity, unit,
                dateOccurred, reason, performedBy)
               VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)""",
            (lotID, movementType, quantity, unit, datetime.now(), reason, userID),
        )
        connection.commit()
        return True, "Stock movement recorded successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Stock adjustment error:", repr(error))
        return False, "The stock movement could not be saved. Please try again."
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def calculateLotAvailability(lotID):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT movementType, quantity
               FROM StockMovements
               WHERE lotID = %s
               ORDER BY dateOccurred""",
            (lotID,)
        )

        movements = cursor.fetchall()

        stockOnHand = 0
        reservedQuantity = 0

        for movementType, quantity in movements:
            movementType = str(
                movementType
            ).strip().lower()

            quantity = float(quantity)

            if movementType == "added":
                stockOnHand += quantity

            elif movementType == "reserved":
                reservedQuantity += quantity

            elif movementType == "delivered":
                stockOnHand -= quantity
                reservedQuantity -= quantity

            elif movementType == "loss":
                stockOnHand -= quantity

            elif movementType == "canceled":
                reservedQuantity -= quantity

            elif movementType == "correction_added":
                stockOnHand += quantity

            elif movementType == "correction_removed":
                stockOnHand -= quantity

        availableQuantity = (
            stockOnHand - reservedQuantity
        )

        return (
            stockOnHand,
            reservedQuantity,
            availableQuantity
        )

    except Exception as error:
        print("Availability calculation error:", error)
        return None, None, None

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


def calculateRequestProgress(requestID):
    requestData = selectData(
        """SELECT quantityRequested
           FROM MarketRequests
           WHERE requestID = %s""",
        (requestID,),
        show_error=False
    )

    if requestData.empty:
        return None

    requestedQuantity = float(
        requestData.iloc[0]["quantityRequested"]
    )

    receiptData = selectData(
        """SELECT receivedQuantity
           FROM DeliveryReceipts
           WHERE requestID = %s""",
        (requestID,),
        show_error=False
    )

    receivedQuantity = 0.0

    for _, receipt in receiptData.iterrows():
        receivedQuantity += float(receipt["receivedQuantity"])

    allocationData = selectData(
        """SELECT allocationID, quantityAllocated, allocStatus
           FROM Allocations
           WHERE requestID = %s""",
        (requestID,),
        show_error=False
    )

    allocatedQuantity = 0.0
    outstandingReserved = 0.0

    for _, allocation in allocationData.iterrows():
        allocationID = int(allocation["allocationID"])
        allocationStatus = str(allocation["allocStatus"])

        if allocationStatus != "canceled":
            allocatedQuantity += float(
                allocation["quantityAllocated"]
            )

        movements = selectData(
            """SELECT movementType, quantity
               FROM StockMovements
               WHERE allocationID = %s
               ORDER BY dateOccurred""",
            (allocationID,),
            show_error=False
        )

        allocationReserved = 0.0

        for _, movement in movements.iterrows():
            movementType = str(movement["movementType"]).lower()
            movementQuantity = float(movement["quantity"])

            if movementType == "reserved":
                allocationReserved += movementQuantity
            elif movementType in {"delivered", "canceled"}:
                allocationReserved -= movementQuantity

        outstandingReserved += max(0.0, allocationReserved)

    remainingQuantity = max(
        0.0,
        requestedQuantity - receivedQuantity - outstandingReserved
    )

    return {
        "requestedQuantity": requestedQuantity,
        "allocatedQuantity": allocatedQuantity,
        "receivedQuantity": receivedQuantity,
        "outstandingReserved": outstandingReserved,
        "remainingQuantity": remainingQuantity
    }


def refreshLotStatus(lotID):
    # Stock availability is calculated; do not change the farmer's lot status.
    return


def formatTableValue(value):
    from decimal import Decimal

    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "—"

    if isinstance(value, (float, Decimal)):
        return f"{float(value):,.2f}".rstrip("0").rstrip(".")

    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M") if (value.hour or value.minute) else value.strftime("%Y-%m-%d")

    return str(value)


def showTable(data, **options):
    # Plain HTML table so headers and cells can be centred and styled.
    from html import escape

    headerHtml = "".join(f"<th>{escape(str(column))}</th>" for column in data.columns)
    rowHtml = "".join(
        "<tr>" + "".join(f"<td>{escape(formatTableValue(value))}</td>" for value in row) + "</tr>"
        for row in data.itertuples(index=False, name=None)
    )
    st.markdown(
        f'<div class="hl-table-wrap"><table class="hl-table"><thead><tr>{headerHtml}</tr></thead>'
        f"<tbody>{rowHtml}</tbody></table></div>",
        unsafe_allow_html=True
    )


def formatQuantity(quantity, unit):
    return f"{float(quantity):,.2f}".rstrip("0").rstrip(".") + f" {unit}"


STAT_ICONS = {
    "basket": '<path d="M4 10h16l-1.6 9.2a2 2 0 0 1-2 1.8H7.6a2 2 0 0 1-2-1.8L4 10zM8 10l4-6 4 6"/>',
    "bookmark": '<path d="M7 3h10a1 1 0 0 1 1 1v17l-6-4-6 4V4a1 1 0 0 1 1-1z"/>',
    "inbox": '<path d="M3 13l3-8h12l3 8v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6zM3 13h5l1.5 3h5L16 13h5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "sprout": '<path d="M12 21v-9M12 12c0-4 3-7 8-7 0 5-3 8-8 7zM12 14c0-3-2.5-5.5-7-5.5 0 4 2.5 6.5 7 5.5z"/>',
}


def showStatCards(cards):
    from html import escape

    cardHtml = "".join(
        f'<div class="hl-stat hl-stat-{tone}"><div class="hl-stat-top"><span class="hl-stat-label">{escape(label)}</span>'
        f'<span class="hl-stat-icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{STAT_ICONS.get(icon, "")}</svg></span></div>'
        f'<span class="hl-stat-value">{escape(str(value))}</span>'
        f'<span class="hl-stat-note">{escape(note)}</span></div>'
        for label, value, note, tone, icon in cards
    )
    st.markdown(f'<div class="hl-stats">{cardHtml}</div>', unsafe_allow_html=True)


def produceImageData(lotIDs, cropName, cropImages):
    if cropImages.empty:
        return None

    candidates = pd.concat([
        cropImages[cropImages["lotID"].isin(lotIDs)],
        cropImages[cropImages["cropName"] == cropName],
    ])
    for imageName in candidates["imageName"]:
        data = showcaseImageData(imageName)
        if data:
            return data
    return None


def showProduceCards(cropTotals, lots, cropImages):
    from html import escape

    cards = []
    for row in cropTotals.itertuples(index=False):
        lotIDs = lots[(lots["cropName"] == row.cropName) & (lots["unit"] == row.unit)]["lotID"].tolist()
        imageData = produceImageData(lotIDs, row.cropName, cropImages)
        stock = float(row.stock)
        reserved = float(row.reserved)
        availablePercent = max(0, min(100, (float(row.availableNow) / stock * 100) if stock > 0 else 0))
        reservedPercent = max(0, min(100 - availablePercent, (reserved / stock * 100) if stock > 0 else 0))

        if imageData:
            photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{escape(row.cropName)}">'
        else:
            photo = f'<span class="hl-produce-initial">{escape(row.cropName[:1])}</span>'

        sellBy = pd.to_datetime(row.nextSellBy, errors="coerce")
        if pd.isna(sellBy):
            sellByTag = '<span class="hl-produce-tag">No sell-by date</span>'
        else:
            daysLeft = (sellBy.date() - date.today()).days
            left = "last day" if daysLeft <= 0 else "1 day left" if daysLeft == 1 else f"{daysLeft} days left"
            tone = " hl-produce-tag-warn" if daysLeft <= 7 else ""
            sellByTag = f'<span class="hl-produce-tag{tone}">Sell by {sellBy.day} {sellBy.strftime("%b")} · {left}</span>'

        lotWord = "lot" if row.lots == 1 else "lots"
        cards.append(
            f'<article class="hl-produce"><div class="hl-produce-photo">{photo}{sellByTag}</div>'
            f'<div class="hl-produce-body"><div class="hl-produce-head"><span class="hl-produce-name">{escape(row.cropName)}</span>'
            f'<span class="hl-produce-meta">{row.lots} {lotWord} · {escape(str(row.unit))}</span></div>'
            f'<div class="hl-produce-available"><b>{escape(formatQuantity(row.availableNow, row.unit))}</b> available</div>'
            f'<div class="hl-produce-bar" title="Available vs reserved">'
            f'<span class="hl-bar-available" style="width:{availablePercent:.1f}%"></span>'
            f'<span class="hl-bar-reserved" style="width:{reservedPercent:.1f}%"></span></div>'
            f'<div class="hl-produce-legend"><span><i class="hl-dot-available"></i>{escape(formatQuantity(row.availableNow, row.unit))} free</span>'
            f'<span><i class="hl-dot-reserved"></i>{escape(formatQuantity(row.reserved, row.unit))} reserved</span></div>'
            f'<div class="hl-produce-foot">{escape(formatQuantity(row.stock, row.unit))} in stock</div></div></article>'
        )

    st.markdown(f'<div class="hl-produce-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def keepStockView():
    # Clicking the selected option would clear it; keep the last choice instead.
    if st.session_state.get("hl_stock_view") is None:
        st.session_state["hl_stock_view"] = st.session_state.get("hl_stock_view_last", "Cards")
    st.session_state["hl_stock_view_last"] = st.session_state["hl_stock_view"]


def showNeedsAttention(expiringLots, openRequests):
    from html import escape

    def daysText(days):
        return "Last day" if days <= 0 else "1 day left" if days == 1 else f"{days} days left"

    expiringRows = "".join(
        f'<li><span class="hl-attn-main"><b>{escape(str(lot.cropName))}</b> · Lot #{lot.lotID}'
        f'<small>{escape(formatQuantity(lot.availableQuantity, lot.unit))} available · {escape(str(lot.farmName))}</small></span>'
        f'<span class="hl-attn-pill hl-attn-pill-warn">{daysText(lot.daysLeft)}</span></li>'
        for lot in expiringLots.head(4).itertuples(index=False)
    )
    requestRows = "".join(
        f'<li><span class="hl-attn-main"><b>{escape(str(request.cropName))}</b> · {escape(formatQuantity(request.remaining, request.unit))} still needed'
        f'<small>{escape(str(request.marketName))} · {escape(str(request.requestedBy))}</small></span>'
        f'<span class="hl-attn-pill">Request #{request.requestID}</span></li>'
        for request in openRequests.head(4).itertuples(index=False)
    )

    if not expiringRows and not requestRows:
        st.markdown(
            '<div class="hl-attn hl-attn-clear"><span class="hl-attn-title">All clear</span>'
            '<p>Nothing is expiring this week and there are no open requests for your crops.</p></div>',
            unsafe_allow_html=True
        )
        return

    st.subheader("Needs attention")
    with st.container(key="hl_attention"):
        expiringCol, requestsCol = st.columns(2, gap="medium")

        with expiringCol:
            with st.container(key="hl_attn_expiring"):
                st.markdown(
                    f'<div class="hl-attn-head"><span class="hl-attn-title">Expiring this week</span>'
                    f'<span class="hl-attn-count hl-attn-count-warn">{len(expiringLots)}</span></div>'
                    + (f'<ul class="hl-attn-list">{expiringRows}</ul>' if expiringRows
                       else '<p class="hl-attn-empty">No lots reach their sell-by date in the next 7 days.</p>'),
                    unsafe_allow_html=True
                )
                if expiringRows:
                    st.button(
                        "Allocate before they expire", icon=":material/arrow_forward:", width="stretch",
                        key="hl_attn_expiring_go", on_click=goToPage, args=("Requests and allocations",)
                    )

        with requestsCol:
            with st.container(key="hl_attn_requests"):
                st.markdown(
                    f'<div class="hl-attn-head"><span class="hl-attn-title">Open requests for your crops</span>'
                    f'<span class="hl-attn-count">{len(openRequests)}</span></div>'
                    + (f'<ul class="hl-attn-list">{requestRows}</ul>' if requestRows
                       else '<p class="hl-attn-empty">No open market requests for the crops you grow.</p>'),
                    unsafe_allow_html=True
                )
                if requestRows:
                    st.button(
                        "Allocate stock to requests", icon=":material/arrow_forward:", width="stretch",
                        key="hl_attn_requests_go", on_click=goToPage, args=("Requests and allocations",)
                    )


def showUpcomingHarvests(expected, limit=6):
    from html import escape

    rows = []
    for cycle in expected.head(limit).itertuples(index=False):
        harvestDate = pd.to_datetime(cycle.expectedHarvestDate, errors="coerce")
        if pd.isna(harvestDate):
            dayText, monthText, whenText, tone = "—", "", "No date set", ""
        else:
            days = (harvestDate.date() - date.today()).days
            dayText, monthText = str(harvestDate.day), harvestDate.strftime("%b")
            if days < 0:
                whenText, tone = f"{-days} day{'s' if days != -1 else ''} overdue", " hl-attn-pill-warn"
            elif days == 0:
                whenText, tone = "Today", " hl-attn-pill-warn"
            elif days == 1:
                whenText, tone = "Tomorrow", ""
            else:
                whenText, tone = f"In {days} days", ""

        rows.append(
            f'<li><span class="hl-harvest-date"><b>{dayText}</b><small>{monthText}</small></span>'
            f'<span class="hl-attn-main"><b>{escape(str(cycle.cropName))}</b> · {escape(formatQuantity(cycle.expectedYield, cycle.expectedUnit))} expected'
            f'<small>{escape(str(cycle.farmName))} · Crop cycle #{cycle.cycleID}</small></span>'
            f'<span class="hl-attn-pill{tone}">{whenText}</span></li>'
        )

    more = len(expected) - limit
    moreHtml = f'<p class="hl-harvest-more">+ {more} more on the Farms page</p>' if more > 0 else ""
    st.markdown(
        f'<div class="hl-harvests"><ul class="hl-attn-list">{"".join(rows)}</ul>{moreHtml}</div>',
        unsafe_allow_html=True
    )


def goToPage(page):
    st.session_state["hl_nav_farmer"] = page


def showWelcomeBand(current_user):
    from html import escape

    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    firstName = str(current_user.get("fname") or current_user["fullname"]).split()[0]
    today = date.today()

    with st.container(key="hl_welcome"):
        textCol, actionCol = st.columns([0.62, 0.38], vertical_alignment="center")

        with textCol:
            st.markdown(
                f'<div class="hl-welcome-date">{today.strftime("%A")}, {today.day} {today.strftime("%B %Y")}</div>'
                f'<h1 class="hl-welcome-title">{greeting}, <em>{escape(firstName)}</em></h1>'
                '<p class="hl-welcome-sub">Here is how your harvests and market requests look today.</p>',
                unsafe_allow_html=True
            )

        with actionCol:
            recordCol, requestsCol = st.columns(2)
            recordCol.button(
                "Record harvest", icon=":material/add:", type="primary", width="stretch",
                key="hl_quick_record", on_click=goToRecordHarvest
            )
            requestsCol.button(
                "View requests", icon=":material/inbox:", width="stretch",
                key="hl_quick_requests", on_click=goToPage, args=("Requests and allocations",)
            )


def showDashboard(current_user):
    showWelcomeBand(current_user)

    available = selectData(
        """SELECT hl.lotID, f.farmName, f.location, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
           FROM HarvestLots hl, CropCycle cc, Farms f, Crops c, OwnedBy o
           WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.farmID = o.farmID AND o.userID = %s AND hl.harvestDate <= %s AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s) AND hl.status = 1
           ORDER BY hl.sellByDate, c.cropName, f.farmName""",(current_user["userID"], date.today(), date.today())
    )

    expected = selectData(
        """SELECT cc.cycleID, f.farmName, c.cropName, cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
           FROM CropCycle cc, Farms f, Crops c, OwnedBy o
           WHERE cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.farmID = o.farmID AND o.userID = %s AND cc.status = 1
           ORDER BY cc.expectedHarvestDate""",
        (current_user["userID"],)
    )
    openRequests = selectData(
        """SELECT mr.requestID, CONCAT(u.fname, ' ', u.lname) AS requestedBy, m.marketName, c.cropName, mr.unit, mr.requestDate
           FROM MarketRequests mr, Users u, Markets m, Crops c
           WHERE mr.userID = u.userID AND mr.marketID = m.marketID AND mr.cropID = c.cropID
             AND mr.reqStatus IN ('pending', 'partiallyFulfilled') AND mr.cropID IN (
               SELECT cc.cropID FROM CropCycle cc, OwnedBy o
               WHERE cc.farmID = o.farmID AND o.userID = %s AND cc.status = 1)
           ORDER BY mr.requestDate""",
        (current_user["userID"],)
    )
    if not openRequests.empty:
        openRequests["remaining"] = [
            (calculateRequestProgress(int(requestID)) or {}).get("remainingQuantity", 0)
            for requestID in openRequests["requestID"]
        ]
        openRequests = openRequests[openRequests["remaining"] > 0]
    openRequestCount = len(openRequests)

    if available.empty:
        available = pd.DataFrame(columns=[
            "lotID", "farmName", "location", "cropName", "quantityHarvested",
            "unit", "harvestDate", "sellByDate"
        ])

    for column in ["stockOnHand", "reservedQuantity", "availableQuantity"]:
        available[column] = 0.0

    for index, lot in available.iterrows():
        stockOnHand, reservedQuantity, availableQuantity = calculateLotAvailability(int(lot["lotID"]))

        if availableQuantity is None:
            continue

        available.at[index, "stockOnHand"] = float(stockOnHand)
        available.at[index, "reservedQuantity"] = float(reservedQuantity)
        available.at[index, "availableQuantity"] = float(availableQuantity)

    inStock = available[available["availableQuantity"] > 0].copy()
    reservedLots = int((available["reservedQuantity"] > 0).sum())

    expectedDates = pd.to_datetime(expected["expectedHarvestDate"], errors="coerce").dt.date if not expected.empty else pd.Series(dtype=object)
    upcomingHarvests = int(((expectedDates >= date.today()) & (expectedDates <= date.today() + timedelta(days=7))).sum())

    sellBy = pd.to_datetime(available["sellByDate"], errors="coerce").dt.date
    expiringSoon = int(((sellBy >= date.today()) & (sellBy <= date.today() + timedelta(days=7))).sum())

    showStatCards([
        ("Available lots", len(inStock), "Stock left to allocate", "green", "basket"),
        ("Reserved lots", reservedLots, "Holding stock for market requests", "leaf", "bookmark"),
        ("Open market requests", openRequestCount, "For crops you grow", "sand", "inbox"),
        ("Expiring this week", expiringSoon, "Sell-by date in the next 7 days", "warn" if expiringSoon else "sand", "clock"),
        ("Upcoming harvests", upcomingHarvests, "Expected in the next 7 days", "leaf", "sprout"),
    ])

    expiringLots = available[(sellBy >= date.today()) & (sellBy <= date.today() + timedelta(days=7))].copy()
    expiringLots["daysLeft"] = [(day - date.today()).days for day in sellBy[expiringLots.index]]
    showNeedsAttention(expiringLots.sort_values("daysLeft"), openRequests)

    if available.empty:
        st.info("No harvested lots are available yet.")

    else:
        # Lots without a sell-by date hold NaN, so compare as datetimes.
        groupable = available.assign(
            sellByDate=pd.to_datetime(available["sellByDate"], errors="coerce")
        )
        cropTotals = (
            groupable.groupby(["cropName", "unit"])
            .agg(
                lots=("lotID", "count"),
                stock=("stockOnHand", "sum"),
                reserved=("reservedQuantity", "sum"),
                availableNow=("availableQuantity", "sum"),
                nextSellBy=("sellByDate", "min"),
            )
            .reset_index()
            .sort_values("availableNow", ascending=False)
        )
        cropTotals["nextSellBy"] = [
            None if pd.isna(day) else day.date() for day in cropTotals["nextSellBy"]
        ]

        titleCol, viewCol = st.columns([0.7, 0.3], vertical_alignment="bottom")
        with titleCol:
            st.subheader("Available produce")
            st.caption("Your current harvest inventory, grouped by crop and unit.")
        with viewCol:
            stockView = st.segmented_control(
                "Stock view",
                ["Cards", "Table"],
                default="Cards",
                key="hl_stock_view",
                on_change=keepStockView,
                format_func=lambda option: {"Cards": ":material/grid_view: Cards", "Table": ":material/table_rows: Table"}[option],
                label_visibility="collapsed",
            )

        if stockView == "Table":
            showTable(
                pd.DataFrame({
                    "Crop": cropTotals["cropName"],
                    "Lots": cropTotals["lots"],
                    "In stock": [formatQuantity(q, u) for q, u in zip(cropTotals["stock"], cropTotals["unit"])],
                    "Reserved": [formatQuantity(q, u) for q, u in zip(cropTotals["reserved"], cropTotals["unit"])],
                    "Available": [formatQuantity(q, u) for q, u in zip(cropTotals["availableNow"], cropTotals["unit"])],
                    "Next sell-by": cropTotals["nextSellBy"],
                })
            )

        cropImages = pd.DataFrame() if stockView == "Table" else selectData(
            """SELECT hi.lotID, c.cropName, hi.imageName
               FROM HarvestLotImages hi
               JOIN HarvestLots hl ON hi.lotID = hl.lotID
               JOIN CropCycle cc ON hl.cycleID = cc.cycleID
               JOIN Crops c ON cc.cropID = c.cropID
               LEFT JOIN OwnedBy o ON o.farmID = cc.farmID AND o.userID = %s
               ORDER BY (o.userID IS NULL), hl.harvestDate DESC, hi.imageNumber""",
            (current_user["userID"],)
        )
        if stockView != "Table":
            showProduceCards(cropTotals, available, cropImages)

        def lotAmounts(column):
            return [formatQuantity(q, u) for q, u in zip(available[column], available["unit"])]

        with st.expander("View detailed inventory", icon=":material/inventory_2:"):
            showTable(
                pd.DataFrame({
                    "Lot #": available["lotID"],
                    "Farm": available["farmName"],
                    "Location": available["location"],
                    "Crop": available["cropName"],
                    "Harvested": lotAmounts("quantityHarvested"),
                    "Harvest date": available["harvestDate"],
                    "Sell-by date": available["sellByDate"],
                    "In stock": lotAmounts("stockOnHand"),
                    "Reserved": lotAmounts("reservedQuantity"),
                    "Available": lotAmounts("availableQuantity"),
                }),
                width="stretch",
                hide_index=True
            )

    st.subheader("Upcoming harvests")

    if expected.empty:
        st.info("No expected harvests have been entered.")

    else:
        showUpcomingHarvests(expected)

def createProduceRequest(
    current_user,
    cropID,
    marketID,
    quantity,
    unit
):
    connection = None
    cursor = None

    try:
        if current_user["role"] != "user":
            logActivity(
                current_user["userID"],
                "access_denied",
                "Attempted to create a market request"
            )
            return False, (
                "Only market buyers can create requests."
            )

        if quantity <= 0:
            return False, (
                "The requested quantity must be greater than zero."
            )

        unit = str(unit).strip().lower()

        if not isValidUnit(unit):
            return False, "Choose a valid unit."

        connection = getConnection()
        cursor = connection.cursor()

        # Re-check current status so stale browser forms cannot create requests.
        cursor.execute(
            "SELECT cropID FROM Crops WHERE cropID = %s AND isActive = 1 FOR UPDATE",
            (cropID,)
        )
        if cursor.fetchone() is None:
            return False, "This crop is inactive. Please choose another crop."
        cursor.execute(
            "SELECT marketID FROM Markets WHERE marketID = %s AND isActive = 1 FOR UPDATE",
            (marketID,)
        )
        if cursor.fetchone() is None:
            return False, "This market is inactive. Please choose another market."

        requestDate = datetime.now()

        cursor.execute(
            """INSERT INTO MarketRequests
               (userID, marketID, cropID,
                quantityRequested, unit,
                requestDate, reqStatus)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                current_user["userID"],
                marketID,
                cropID,
                quantity,
                unit,
                requestDate,
                "pending"
            )
        )

        requestID = cursor.lastrowid

        cursor.execute(
            """INSERT INTO RequestStatusHistory
               (requestID, oldStatus, newStatus,
                changedAt, changedBy, reason)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                requestID,
                None,
                "pending",
                requestDate,
                current_user["userID"],
                "Market request created"
            )
        )

        connection.commit()

        return True, "Your request was submitted successfully."

    except Exception as error:
        if connection:
            connection.rollback()

        print("Request creation error:", error)

        return False, (
            "The request could not be submitted."
        )

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()

def shopSellByTag(sellByDate):
    sellBy = pd.to_datetime(sellByDate, errors="coerce")
    if pd.isna(sellBy):
        return '<span class="hl-produce-tag">No sell-by date</span>'
    daysLeft = (sellBy.date() - date.today()).days
    left = "last day" if daysLeft <= 0 else "1 day left" if daysLeft == 1 else f"{daysLeft} days left"
    tone = " hl-produce-tag-warn" if daysLeft <= 7 else ""
    return f'<span class="hl-produce-tag{tone}">Sell by {sellBy.day} {sellBy.strftime("%b")} · {left}</span>'


@st.cache_data(show_spinner=False)
def shopPhotoData(imageName):
    import base64
    from io import BytesIO
    from PIL import ImageOps

    imagePath = resolveHarvestImagePath(imageName)
    if imagePath is None:
        return None
    try:
        with Image.open(imagePath) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, (960, 720))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as error:
        print(f"Listing image failed ({imageName!r}): {error!r}")
        return None


def shopCardHtml(lot, imageName):
    from html import escape

    crop = escape(str(lot.cropName))
    imageData = showcaseImageData(imageName) if imageName else None
    if imageData:
        photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{crop}">'
    else:
        photo = f'<span class="hl-produce-initial">{escape(str(lot.cropName)[:1])}</span>'

    sellByTag = shopSellByTag(lot.sellByDate)
    harvested = readableDate(lot.harvestDate) or "date not set"
    icon = '<span class="material-symbols-rounded" aria-hidden="true">{}</span>'
    return (
        f'<article class="hl-produce hl-shop"><div class="hl-produce-photo">{photo}{sellByTag}</div>'
        f'<div class="hl-produce-body"><div class="hl-produce-head"><span class="hl-produce-name">{crop}</span>'
        f'<span class="hl-produce-meta">Lot #{int(lot.lotID)}</span></div>'
        f'<div class="hl-produce-available"><b>{escape(formatQuantity(lot.availableQuantity, lot.unit))}</b> available</div>'
        f'<div class="hl-shop-foot"><span>{icon.format("agriculture")}{escape(str(lot.farmName))}</span>'
        f'<span>{icon.format("location_on")}{escape(str(lot.location))}</span>'
        f'<span>{icon.format("calendar_today")}Harvested {harvested}</span></div></div></article>'
    )


def showProduceSearch(current_user):
    st.header("Local produce marketplace")

    if current_user["role"] != "user":
        denyAccess(current_user, "Attempted to open the buyer produce marketplace")
        return

    if "selectedProduceLot" not in st.session_state:
        st.session_state.selectedProduceLot = None

    crops = selectData(
        """SELECT cropID, cropName
           FROM Crops
           WHERE isActive = 1
           ORDER BY cropName"""
    )

    farms = selectData(
        """SELECT farmID, farmName
           FROM Farms
           ORDER BY farmName"""
    )

    if not crops.empty:
        cropNames = ["All"] + crops["cropName"].tolist()
    else:
        cropNames = ["All"]

    if not farms.empty:
        farmNames = ["All"] + farms["farmName"].tolist()
    else:
        farmNames = ["All"]

    units = getActiveUnits()

    if not units:
        st.warning("No supported units are configured in the application.")
        return

    # Show the filters only while viewing the listing page.
    if st.session_state.selectedProduceLot is None:
        with st.container(
            horizontal=True,
            vertical_alignment="center",
            gap="small",
            key="shop_filters"
        ):
            selectedCrop = st.pills(
                "Crop",
                cropNames,
                default="All",
                key="shop_crop",
                label_visibility="collapsed"
            ) or "All"

            with st.popover("More filters", icon=":material/tune:"):
                selectedFarm = st.selectbox("Farm", farmNames, key="shop_farm")
                location = st.text_input("Location or emirate", key="shop_location")
                selectedUnit = st.selectbox("Unit", ["All"] + units, key="shop_unit")

                # Pick the day the buyer wants the produce to be available.
                availableOn = st.date_input(
                    "Available on",
                    value=date.today(),
                    min_value=date.today(),
                    key="shop_date"
                )
                minimumQuantity = st.number_input(
                    "Minimum available",
                    min_value=0.0,
                    key="shop_min"
                )

        produceQuery = """SELECT hl.lotID, cc.cropID, f.farmName, f.location, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
                        FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
                        WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND c.isActive = 1 AND hl.status = 1 AND hl.harvestDate <= %s AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s)"""
        values = [
            availableOn,
            availableOn
        ]

        if selectedCrop != "All":
            produceQuery += " AND c.cropName = %s"
            values.append(selectedCrop)

        if selectedFarm != "All":
            produceQuery += " AND f.farmName = %s"
            values.append(selectedFarm)

        if location.strip():
            produceQuery += " AND f.location LIKE %s"
            values.append(f"%{location.strip()}%")

        if selectedUnit != "All":
            produceQuery += " AND hl.unit = %s"
            values.append(selectedUnit)


        produceQuery += " ORDER BY hl.sellByDate, c.cropName, f.farmName"

        available = selectData(produceQuery, tuple(values))

        if not available.empty:
            available["stockOnHand"] = 0.0
            available["reservedQuantity"] = 0.0
            available["availableQuantity"] = 0.0

            for index, lot in available.iterrows():
                lotID = int(lot["lotID"])

                stockOnHand, reservedQuantity, availableQuantity = (
                    calculateLotAvailability(lotID)
                )

                if availableQuantity is None:
                    continue

                available.at[index, "stockOnHand"] = float(stockOnHand)
                available.at[index, "reservedQuantity"] = float(
                    reservedQuantity
                )
                available.at[index, "availableQuantity"] = float(
                    availableQuantity
                )

            available = available[available["availableQuantity"] >= minimumQuantity]

            available = available[ available["availableQuantity"] > 0]

        filterNotes = []
        if selectedFarm != "All":
            filterNotes.append(selectedFarm)
        if location.strip():
            filterNotes.append(f'"{location.strip()}"')
        if selectedUnit != "All":
            filterNotes.append(f"in {selectedUnit}")
        if availableOn != date.today():
            filterNotes.append(f"available on {readableDate(availableOn)}")
        if minimumQuantity > 0:
            filterNotes.append(f"at least {minimumQuantity:,g} available")

        if available.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'search_off</span><span>No produce matched these filters. Try another crop or '
                'change More filters.</span></div>',
                unsafe_allow_html=True
            )
            return

        listingWord = "listing" if len(available) == 1 else "listings"
        st.subheader(f"{len(available)} {listingWord} available", anchor=False)
        if filterNotes:
            st.caption("Filtered by " + " · ".join(filterNotes))

        lotIDs = [int(lotID) for lotID in available["lotID"]]
        photos = selectData(
            f"""SELECT lotID, imageName FROM HarvestLotImages
                WHERE lotID IN ({", ".join(["%s"] * len(lotIDs))})
                ORDER BY lotID, imageNumber""",
            tuple(lotIDs),
            show_error=False
        )
        firstPhoto = {} if photos.empty else {
            int(lotID): imageName
            for lotID, imageName in photos.groupby("lotID")["imageName"].first().items()
        }

        lots = list(available.itertuples(index=False))
        for start in range(0, len(lots), 3):
            columns = st.columns(3)
            for column, lot in zip(columns, lots[start:start + 3]):
                lotID = int(lot.lotID)
                with column, st.container(key=f"shop_card_{lotID}"):
                    st.markdown(
                        shopCardHtml(lot, firstPhoto.get(lotID)),
                        unsafe_allow_html=True
                    )
                    if st.button(
                        "View & request",
                        key=f"view_listing_{lotID}",
                        type="primary",
                        icon=":material/arrow_forward:",
                        width="stretch"
                    ):
                        st.session_state.selectedProduceLot = lotID
                        st.rerun()

    # Display the selected listing.
    else:
        from html import escape

        selectedLotID = st.session_state.selectedProduceLot

        if st.button(
            "Back to listings",
            key="shop_back",
            type="tertiary",
            icon=":material/arrow_back:"
        ):
            st.session_state.selectedProduceLot = None
            st.rerun()

        listing = selectData( """SELECT hl.lotID, cc.cropID, f.farmName, f.location,  c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
            FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
            WHERE hl.lotID = %s AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND c.isActive = 1 AND hl.status = 1""",(selectedLotID,) )

        if listing.empty:
            st.error("This produce listing is no longer available.")
            st.session_state.selectedProduceLot = None
            return

        lot = listing.iloc[0]

        stockOnHand, reservedQuantity, availableQuantity = (
            calculateLotAvailability(selectedLotID)
        )
        available = float(availableQuantity or 0)
        unit = str(lot["unit"])
        crop = str(lot["cropName"])
        icon = '<span class="material-symbols-rounded" aria-hidden="true">{}</span>'

        images = selectData(
            """SELECT imageNumber, imageName
               FROM HarvestLotImages
               WHERE lotID = %s
               ORDER BY imageNumber""",
            (selectedLotID,)
        )
        imageNames = [] if images.empty else images["imageName"].tolist()

        imageColumn, detailColumn = st.columns([1.15, 1], gap="large")

        with imageColumn:
            photoKey = f"shop_photo_{selectedLotID}"
            photoIndex = st.session_state.get(photoKey) or 0
            photoIndex = min(photoIndex, max(len(imageNames) - 1, 0))
            imageData = shopPhotoData(imageNames[photoIndex]) if imageNames else None

            if imageData:
                photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{escape(crop)}">'
            else:
                photo = (
                    f'<span class="hl-produce-initial">{escape(crop[:1])}</span>'
                    '<span class="hl-listing-nophoto">No photos of this lot yet</span>'
                )

            st.markdown(
                f'<div class="hl-listing-photo">{photo}{shopSellByTag(lot["sellByDate"])}</div>',
                unsafe_allow_html=True
            )

            if len(imageNames) > 1:
                st.segmented_control(
                    "Photo",
                    list(range(len(imageNames))),
                    format_func=lambda index: f"Photo {index + 1}",
                    default=0,
                    key=photoKey,
                    label_visibility="collapsed"
                )

        with detailColumn:
            facts = [
                ("agriculture", "Farm", lot["farmName"]),
                ("location_on", "Location", lot["location"]),
                ("calendar_today", "Harvested", readableDate(lot["harvestDate"]) or "Not set"),
                ("event_busy", "Sell by", readableDate(lot["sellByDate"]) or "No sell-by date"),
            ]
            factHtml = "".join(
                f'<div class="hl-listing-fact">{icon.format(name)}<span><small>{label}</small>{escape(str(value))}</span></div>'
                for name, label, value in facts
            )
            st.markdown(
                f'<div class="hl-listing-head"><span class="hl-eyebrow">Fresh from {escape(str(lot["farmName"]))}</span>'
                f'<h1>{escape(crop)}</h1><span class="hl-listing-sub">Lot #{int(selectedLotID)}</span>'
                f'<div class="hl-listing-stock"><b>{escape(formatQuantity(available, unit))}</b> available now</div>'
                f'<div class="hl-listing-facts">{factHtml}</div></div>',
                unsafe_allow_html=True
            )

            with st.container(key="shop_request_panel"):
                st.markdown(
                    f'<div class="hl-listing-panel-head"><strong>Request {escape(crop.lower())}</strong>'
                    f'<span>Sold in {escape(unit)}. Any farm with {escape(crop.lower())} in {escape(unit)} can fill your request.</span></div>',
                    unsafe_allow_html=True
                )

                if available < 0.01:
                    st.info("This lot is fully reserved right now. Check back later or pick another listing.")
                    return

                markets = selectData(
                    """SELECT marketID, marketName, location
                       FROM Markets
                       WHERE isActive = 1
                       ORDER BY marketName"""
                )

                if markets.empty:
                    st.warning("No markets are available.")
                    return

                marketOptions = {}

                for _, market in markets.iterrows():
                    label = (
                        f"{market['marketName']} · "
                        f"{market['location']}"
                    )
                    marketOptions[label] = int(market["marketID"])

                with st.form("produce_request_form", border=False):
                    selectedMarket = st.selectbox(
                        "Deliver to",
                        list(marketOptions.keys())
                    )

                    requestedQuantity = st.number_input(
                        f"Amount ({unit})",
                        min_value=0.01,
                        max_value=available,
                        value=min(1.0, available),
                        help=f"Up to {formatQuantity(available, unit)} is available from this lot."
                    )

                    submitted = st.form_submit_button(
                        "Send request",
                        type="primary",
                        icon=":material/shopping_basket:",
                        width="stretch"
                    )

                if submitted:
                    # Recalculate in case availability changed while
                    # the buyer was viewing the page.
                    _, _, latestAvailability = calculateLotAvailability(
                        selectedLotID
                    )

                    if latestAvailability is None:
                        st.warning("Availability could not be checked. Please try again.")

                    elif requestedQuantity > float(latestAvailability):
                        st.warning(
                            "The available quantity has changed. "
                            "Please enter a smaller quantity."
                        )

                    else:
                        created, message = createProduceRequest(
                            current_user=current_user,
                            cropID=int(lot["cropID"]),
                            marketID=marketOptions[selectedMarket],
                            quantity=requestedQuantity,
                            unit=unit
                        )

                        if created:
                            st.success(message)
                            st.button(
                                "See my requests",
                                key="shop_go_requests",
                                icon=":material/arrow_forward:",
                                on_click=lambda: st.session_state.update(
                                    {"hl_nav_user": "My requests", "selectedProduceLot": None}
                                )
                            )
                        else:
                            st.error(message)
def resolveHarvestImagePath(imageName):
    """Find an image referenced by MySQL in the supported local image folders."""
    imageName = str(imageName).strip()
    if not imageName or Path(imageName).name != imageName:
        return None

    # The first directory is where this application saves new uploads.
    # The second supports projects that kept seeded assets at repo/images/.
    folders = (IMAGE_FOLDER, IMAGE_FOLDER.parent.parent / "images")
    for folder in folders:
        candidate = folder / imageName
        if candidate.is_file():
            return candidate

        # Useful on case-sensitive systems if DB and disk differ in case.
        if folder.is_dir():
            for existing in folder.iterdir():
                if existing.is_file() and existing.name.casefold() == imageName.casefold():
                    return existing
    return None



def showHarvestImageFile(imageName, caption=None, compact=False, **imageOptions):
    """Show an image safely without exposing server paths or decoding errors."""
    try:
        imagePath = resolveHarvestImagePath(imageName)
        if imagePath is None:
            if compact:
                st.caption("Image unavailable.")
            else:
                st.warning("This image is unavailable right now.")
            return False

        # Decode eagerly: broken images may otherwise fail inside st.image().
        with Image.open(imagePath) as source:
            source.load()
            from PIL import ImageOps
            displayImage = ImageOps.exif_transpose(source).copy()

        st.image(displayImage, caption=caption, **imageOptions)
        return True
    except Exception as error:
        # Full diagnostic detail is for developers, never for the UI.
        print(f"Harvest image display failed ({imageName!r}): {error!r}")
        if compact:
            st.caption("Image unavailable.")
        else:
            st.warning("This image cannot be displayed right now.")
        return False


UPLOAD_IMAGE_FORMATS = {"JPEG", "MPO", "PNG"}


def openUploadedImage(uploadedFile):
    # Check the decoded format, not the file name: only the JPEG and PNG
    # decoders are tried, so a GIF or WebP renamed to .png is rejected.
    # MPO is the JPEG variant some phone cameras save.
    uploadedFile.seek(0)
    source = Image.open(uploadedFile, formats=("JPEG", "PNG"))
    if source.format not in UPLOAD_IMAGE_FORMATS:
        source.close()
        raise UnidentifiedImageError(f"Unsupported image format: {source.format}")
    return source


def imagePixelFingerprint(image):
    """Compare image appearance independent of JPG/PNG encoding or metadata."""
    from PIL import ImageOps
    import hashlib

    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width * image.height > 20_000_000:
        raise ValueError("Image dimensions are too large (20 MP maximum).")
    digest = hashlib.sha256()
    digest.update(f"{image.width}x{image.height}:RGB:".encode("ascii"))
    digest.update(image.tobytes())
    return digest.hexdigest()


def selectImageLot(label):
    st.session_state["selected_image_lot"] = label


def keepGalleryView():
    if st.session_state.get("image_gallery_view") is None:
        st.session_state["image_gallery_view"] = "all"


def showLotGallery(lots, labelByLot):
    from html import escape

    missing = int((lots["photoCount"] == 0).sum())
    lotWord = "lot" if len(lots) == 1 else "lots"
    textCol, filterCol = st.columns([0.6, 0.4], vertical_alignment="center")
    with textCol:
        st.markdown(
            f'<p class="hl-gallery-sub">{len(lots)} {lotWord} · '
            f'{missing} still {"needs" if missing == 1 else "need"} a photo</p>',
            unsafe_allow_html=True
        )
    with filterCol:
        with st.container(horizontal=True, horizontal_alignment="right"):
            view = st.segmented_control(
                "Show",
                ["all", "missing"],
                format_func=lambda option: "All lots" if option == "all" else f"Needs a photo ({missing})",
                default="all",
                key="image_gallery_view",
                on_change=keepGalleryView,
                label_visibility="collapsed"
            )

    shown = lots[lots["photoCount"] == 0] if view == "missing" else lots
    if shown.empty:
        st.markdown(
            '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
            'check_circle</span>Every lot has a photo.</div>',
            unsafe_allow_html=True
        )
        return

    firstLabel = next(iter(labelByLot.values()))
    selectedLabel = st.session_state.get("selected_image_lot", firstLabel)

    for start in range(0, len(shown), 4):
        columns = st.columns(4)
        for column, row in zip(columns, shown.iloc[start:start + 4].itertuples(index=False)):
            label = labelByLot[row.lotID]
            selected = label == selectedLabel
            count = int(row.photoCount)
            imageData = showcaseImageData(row.coverImage) if isinstance(row.coverImage, str) else None

            if imageData:
                photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{escape(str(row.cropName))}">'
            else:
                photo = (
                    '<span class="hl-gallery-placeholder"><span class="material-symbols-rounded" '
                    'aria-hidden="true">add_a_photo</span>No photo yet</span>'
                )

            if count == 0:
                badge = '<span class="hl-gallery-count hl-gallery-count-none">No photos</span>'
            else:
                badge = f'<span class="hl-gallery-count">{count} photo{"" if count == 1 else "s"}</span>'

            harvested = pd.to_datetime(row.harvestDate, errors="coerce")
            harvestedText = "—" if pd.isna(harvested) else f"{harvested.day} {harvested.strftime('%b %Y')}"
            selectedClass = " hl-gallery-selected" if selected else ""

            with column, st.container(key=f"image_lot_card_{row.lotID}"):
                st.markdown(
                    f'<div class="hl-gallery-card{selectedClass}"><div class="hl-gallery-photo">{photo}{badge}</div>'
                    f'<div class="hl-gallery-body"><span class="hl-gallery-name">{escape(str(row.cropName))}</span>'
                    f'<span class="hl-gallery-meta">Lot #{row.lotID} · {escape(str(row.farmName))}</span>'
                    f'<span class="hl-gallery-meta">Harvested {harvestedText}</span></div></div>',
                    unsafe_allow_html=True
                )
                st.button(
                    "Selected" if selected else "Manage photos",
                    key=f"image_lot_pick_{row.lotID}",
                    icon=":material/check:" if selected else ":material/photo_library:",
                    type="primary" if selected else "secondary",
                    width="stretch",
                    on_click=selectImageLot,
                    args=(label,)
                )


@st.cache_data(show_spinner=False)
def lotPhotoData(imageName):
    import base64
    from io import BytesIO
    from PIL import ImageOps

    imagePath = resolveHarvestImagePath(imageName)
    if imagePath is None:
        return None
    try:
        with Image.open(imagePath) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, (640, 480))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as error:
        print(f"Lot photo failed ({imageName!r}): {error!r}")
        return None


def showImages(current_user):
    from html import escape

    st.header("Harvest lot images")

    role = current_user["role"]
    user_id = int(current_user["userID"])

    # Farmers see only their own lots.
    if role == "farmer":
        lots = selectData("""SELECT hl.lotID, f.farmName,  c.cropName, hl.harvestDate,
                (SELECT COUNT(*) FROM HarvestLotImages hi WHERE hi.lotID = hl.lotID) AS photoCount,
                (SELECT hi.imageName FROM HarvestLotImages hi WHERE hi.lotID = hl.lotID
                 ORDER BY hi.imageNumber LIMIT 1) AS coverImage
            FROM HarvestLots hl, CropCycle cc, Farms f, Crops c, OwnedBy o
            WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.farmID = o.farmID AND o.userID = %s
            ORDER BY hl.lotID DESC""",(user_id,))

    # Market users and administrators can view all lots.
    else:
        lots = selectData( """ SELECT hl.lotID,  f.farmName, c.cropName,  hl.harvestDate,
                (SELECT COUNT(*) FROM HarvestLotImages hi WHERE hi.lotID = hl.lotID) AS photoCount,
                (SELECT hi.imageName FROM HarvestLotImages hi WHERE hi.lotID = hl.lotID
                 ORDER BY hi.imageNumber LIMIT 1) AS coverImage
            FROM HarvestLots hl,  CropCycle cc, Farms f,Crops c
            WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID
            ORDER BY  hl.lotID DESC""")

    if lots.empty:
        st.info("No harvest lots are available.")
        return

    options = {}
    for row in lots.itertuples(index=False):
        harvested = pd.to_datetime(row.harvestDate, errors="coerce")
        harvestedText = "" if pd.isna(harvested) else f" · harvested {harvested.day} {harvested.strftime('%b %Y')}"
        options[f"{row.cropName} · Lot #{row.lotID} · {row.farmName}{harvestedText}"] = row.lotID

    labelByLot = {lotID: label for label, lotID in options.items()}
    pendingLotID = st.session_state.pop("pendingImageLotID", None)
    if pendingLotID in labelByLot:
        st.session_state["selected_image_lot"] = labelByLot[pendingLotID]
    showLotGallery(lots, labelByLot)

    selected_label = st.selectbox(
        "Select a harvest lot",
        list(options.keys()),
        key="selected_image_lot"
    )

    selected_lot_id = options[selected_label]

    images = selectData("""SELECT hi.imageNumber, hi.imageName, f.farmName,  c.cropName
        FROM HarvestLotImages hi,  HarvestLots hl,CropCycle cc, Farms f, Crops c
        WHERE hi.lotID = %s AND hi.lotID = hl.lotID AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID
        ORDER BY hi.imageNumber""", (selected_lot_id,))

    lot = lots[lots["lotID"] == selected_lot_id].iloc[0]
    photoCount = len(images)
    harvested = pd.to_datetime(lot["harvestDate"], errors="coerce")
    harvestedText = "—" if pd.isna(harvested) else f"{harvested.day} {harvested.strftime('%b %Y')}"
    if photoCount == 0:
        countBadge = '<span class="hl-gallery-count hl-gallery-count-none">No photos</span>'
    else:
        countBadge = f'<span class="hl-gallery-count">{photoCount} photo{"" if photoCount == 1 else "s"}</span>'

    with st.container(key="image_lot_panel"):
        st.markdown(
            f'<div class="hl-lotpanel-head"><div><span class="hl-lotpanel-eyebrow">Selected lot</span>'
            f'<h3>{escape(str(lot["cropName"]))}</h3>'
            f'<span class="hl-gallery-meta">Lot #{selected_lot_id} · {escape(str(lot["farmName"]))} · '
            f'Harvested {harvestedText}</span></div>{countBadge}</div>',
            unsafe_allow_html=True
        )

        if images.empty:
            hint = " Upload the first one below." if role == "farmer" else ""
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                f'add_a_photo</span>No photos for this lot yet.{hint}</div>',
                unsafe_allow_html=True
            )
        else:
            photos = []
            for position, (_, image_row) in enumerate(images.iterrows(), start=1):
                imageData = lotPhotoData(str(image_row["imageName"]))
                if imageData:
                    picture = (
                        f'<img src="data:image/jpeg;base64,{imageData}" '
                        f'alt="{escape(str(lot["cropName"]))}, photo {position}">'
                    )
                else:
                    picture = '<span class="hl-photo-missing">Image unavailable</span>'
                photos.append(
                    f'<figure class="hl-photo"><div class="hl-photo-frame">{picture}</div>'
                    f'<figcaption>Photo {position}</figcaption></figure>'
                )
            st.markdown(f'<div class="hl-photo-grid">{"".join(photos)}</div>', unsafe_allow_html=True)

        uploadedMessage = st.session_state.pop("harvestImageMessage", None)
        if uploadedMessage:
            st.markdown(
                '<div class="hl-gallery-empty hl-upload-done"><span class="material-symbols-rounded" '
                f'aria-hidden="true">check_circle</span>{escape(uploadedMessage)}</div>',
                unsafe_allow_html=True
            )

        # Only farmers can upload harvest images.
        if role == "farmer":
            uploadRound = st.session_state.get("lot_photo_upload_round", 0)
            st.markdown(
                '<div class="hl-upload-head"><strong>Add a photo</strong>'
                '<span>JPG or PNG, up to 5 MB</span></div>',
                unsafe_allow_html=True
            )
            uploaded_file = st.file_uploader(
                "Add a photo",
                type=["jpg", "jpeg", "png"],
                max_upload_size=5,
                key=f"lot_photo_upload_{selected_lot_id}_{uploadRound}",
                label_visibility="collapsed"
            )

            if uploaded_file is not None:
                previewCol, actionCol = st.columns([0.3, 0.7], vertical_alignment="center")
                with previewCol:
                    try:
                        from PIL import ImageOps
                        with openUploadedImage(uploaded_file) as source:
                            preview = ImageOps.exif_transpose(source).convert("RGB")
                            preview.thumbnail((480, 480))
                        st.image(preview, width="stretch")
                    except Exception:
                        st.caption("This file isn't a valid JPG or PNG image.")
                    finally:
                        uploaded_file.seek(0)

                with actionCol:
                    size = uploaded_file.size
                    sizeText = f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"
                    st.markdown(
                        f'<div class="hl-upload-ready"><strong>{escape(uploaded_file.name)}</strong>'
                        f'<span>{sizeText} · will be added to Lot #{selected_lot_id}</span></div>',
                        unsafe_allow_html=True
                    )
                    if st.button(
                        "Upload photo",
                        type="primary",
                        icon=":material/cloud_upload:",
                        key=f"lot_photo_submit_{selected_lot_id}"
                    ):
                        uploaded, message = addHarvestImage(
                            selected_lot_id,
                            uploaded_file,
                            user_id
                        )

                        if uploaded:
                            st.session_state.harvestImageMessage = message
                            st.session_state["lot_photo_upload_round"] = uploadRound + 1
                            st.rerun()
                        else:
                            st.warning(message)


def addHarvestImage(lotID, uploadedFile, userID):
    connection = None
    cursor = None
    savedPath = None
    createdFile = False

    try:
        if uploadedFile is None:
            return False, "Choose an image first."
        if uploadedFile.size > 5 * 1024 * 1024:
            return False, "The image must be 5 MB or smaller."

        # Decode and normalize before comparing or saving. Metadata and
        # different file encodings won't create false non-duplicates.
        try:
            from PIL import ImageOps
            with openUploadedImage(uploadedFile) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.load()
            fingerprint = imagePixelFingerprint(image)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            print("Image upload validation error:", repr(error))
            return False, "Please choose a valid JPG or PNG image (maximum 5 MB)."

        connection = getConnection()
        cursor = connection.cursor()

        # Lock this harvest lot so simultaneous clicks cannot allocate
        # the same image number or bypass the duplicate check.
        cursor.execute(
            """SELECT hl.lotID, c.cropName
               FROM HarvestLots hl, CropCycle cc, Crops c, OwnedBy o
               WHERE hl.cycleID = cc.cycleID
               AND cc.cropID = c.cropID
               AND cc.farmID = o.farmID
               AND hl.lotID = %s
               AND o.userID = %s
               FOR UPDATE""",
            (lotID, userID)
        )
        lotRow = cursor.fetchone()
        if lotRow is None:
            connection.rollback()
            return False, "This harvested lot does not belong to your farm."

        safeCropName = re.sub(
            r"[^a-z0-9]+", "_", str(lotRow[1]).strip().lower()
        ).strip("_") or "produce"

        cursor.execute(
            """SELECT imageNumber, imageName
               FROM HarvestLotImages
               WHERE lotID = %s
               ORDER BY imageNumber""",
            (lotID,)
        )
        existingImages = cursor.fetchall()

        # Duplicate detection is per lot, not across different farms/lots.
        for imageNumber, existingName in existingImages:
            existingPath = resolveHarvestImagePath(existingName)
            if existingPath is None:
                continue  # Legacy DB record with no corresponding file.
            try:
                with Image.open(existingPath) as stored:
                    if imagePixelFingerprint(stored) == fingerprint:
                        connection.rollback()
                        return False, (
                            "This exact image is already uploaded for this harvest lot "
                            f"(image #{imageNumber})."
                        )
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                # An unreadable historical image must not prevent new uploads.
                print("Skipping unreadable existing image:", existingPath)

        imageNumber = max((int(row[0]) for row in existingImages), default=0) + 1
        while True:
            fileName = f"lot_{lotID}_{imageNumber}_{safeCropName}.png"
            savedPath = IMAGE_FOLDER / fileName
            # Never overwrite an orphaned file whose DB record was deleted.
            if not savedPath.exists():
                break
            imageNumber += 1

        # Exclusive file creation guards against accidental overwrites.
        with savedPath.open("xb") as target:
            createdFile = True
            image.save(target, format="PNG")

        cursor.execute(
            """INSERT INTO HarvestLotImages
               (imageNumber, lotID, imageName)
               VALUES (%s, %s, %s)""",
            (imageNumber, lotID, fileName)
        )
        connection.commit()
        logActivity(
            userID, "harvest_image_uploaded",
            f"Uploaded {fileName} for lot {lotID}"
        )
        return True, "Image uploaded successfully."

    except Exception as error:
        if connection:
            connection.rollback()
        if createdFile and savedPath is not None:
            try:
                savedPath.unlink(missing_ok=True)
            except OSError as cleanupError:
                print("Image cleanup error:", repr(cleanupError))
        print("Image upload error:", repr(error))
        return False, "The image could not be uploaded. Please try again."

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()



# -----------------------------------------------------------------------------
# Farmer pages
# -----------------------------------------------------------------------------

def setAddCycleOpen(isOpen):
    st.session_state["fm_add_open"] = isOpen


@st.dialog("Edit crop cycle", width="medium")
def editCropCycleDialog(cycle, farmID, units):
    from html import escape

    cycleID = int(cycle.cycleID)
    st.markdown(
        '<div class="hl-release-head">'
        f'<strong>{escape(str(cycle.cropName))} · Cycle #{cycleID}</strong>'
        '<span>Change what you expect to harvest and when.</span></div>',
        unsafe_allow_html=True
    )

    cycleUnits = list(units)
    currentUnit = str(cycle.expectedUnit)
    if currentUnit not in cycleUnits:
        cycleUnits.append(currentUnit)

    selectedDate = pd.to_datetime(cycle.expectedHarvestDate, errors="coerce")
    selectedDate = date.today() if pd.isna(selectedDate) else selectedDate.date()

    with st.form(f"update_cycle_form_{cycleID}", border=False):
        yieldColumn, unitColumn = st.columns(2)
        updatedExpectedYield = yieldColumn.number_input(
            "Expected quantity",
            min_value=0.01,
            value=float(cycle.expectedYield)
        )

        updatedExpectedUnit = unitColumn.selectbox(
            "Expected unit",
            cycleUnits,
            index=cycleUnits.index(currentUnit)
        )

        updatedExpectedDate = st.date_input(
            "Expected harvest date",
            value=selectedDate
        )

        updateCycleSubmitted = st.form_submit_button(
            "Save changes",
            type="primary",
            icon=":material/check:",
            width="stretch"
        )

    if updateCycleSubmitted:
        updated = changeData(
            "UPDATE CropCycle SET expectedYield = %s, expectedUnit = %s, expectedHarvestDate = %s WHERE cycleID = %s AND farmID = %s",
            (
                updatedExpectedYield,
                updatedExpectedUnit,
                updatedExpectedDate,
                cycleID,
                farmID
            )
        )

        if updated:
            st.session_state.farmManagementMessage = (
                "Crop cycle updated successfully."
            )

            st.rerun()


def cycleHistoryRowHtml(row):
    from html import escape

    expected = pd.to_datetime(row.expectedHarvestDate, errors="coerce")
    dayText, monthText = ("—", "") if pd.isna(expected) else (str(expected.day), expected.strftime("%b").upper())
    lotCount = int(row.lotCount)
    lotText = f"{lotCount} lot{'' if lotCount == 1 else 's'}"
    if lotCount == 0:
        harvestedText, pillText, pillClass = "Nothing harvested", "No harvest", "hl-alloc-pill hl-alloc-pill-off"
    elif int(row.unitCount) == 1:
        harvestedText = f"{formatQuantity(float(row.harvestedTotal), str(row.lotUnit))} harvested from {lotText}"
        pillText, pillClass = "Finished", "hl-alloc-pill hl-alloc-pill-done"
    else:
        harvestedText = f"{lotText} harvested in different units"
        pillText, pillClass = "Finished", "hl-alloc-pill hl-alloc-pill-done"
    yearText = "" if pd.isna(expected) else f" {expected.year}"
    return (
        f'<div class="hl-alloc-row"><div class="hl-alloc-date"><b>{dayText}</b><span>{monthText}</span></div>'
        f'<div class="hl-alloc-main"><strong>{escape(str(row.cropName))} · Cycle #{int(row.cycleID)}</strong>'
        f'<small>Expected {escape(formatQuantity(float(row.expectedYield), str(row.expectedUnit)))}'
        f' on {dayText} {monthText.title()}{yearText}</small>'
        f'<small>{escape(harvestedText)}</small></div>'
        f'<div class="hl-alloc-side"><span class="{pillClass}">{pillText}</span></div></div>'
    )


@st.dialog("Record harvest", width="medium")
def recordCycleHarvestDialog(cycle, units, userID):
    from html import escape

    selectedCycleID = int(cycle.cycleID)
    cycleUnit = str(cycle.expectedUnit)
    expectedDate = readableDate(cycle.expectedHarvestDate) or "no date set"
    st.markdown(
        '<div class="hl-release-head">'
        f'<strong>{escape(str(cycle.cropName))} · Cycle #{selectedCycleID}</strong>'
        f'<span>Expecting {escape(formatQuantity(cycle.expectedYield, cycleUnit))} '
        f'on {expectedDate}. The harvested lot is added to your stock.</span></div>',
        unsafe_allow_html=True
    )

    with st.form(f"record_harvest_{selectedCycleID}", border=False):
        quantityColumn, unitColumn = st.columns(2)
        actualQuantity = quantityColumn.number_input(
            "Actual quantity harvested",
            min_value=0.01,
            value=0.01
        )

        unitIndex = units.index(cycleUnit) if cycleUnit in units else 0
        actualUnit = unitColumn.selectbox(
            "Unit",
            units,
            index=unitIndex,
            help=(
                f"This cycle expects {cycleUnit}. Stock is never converted between "
                f"units, so a lot in another unit won't match {cycleUnit} requests."
            )
        )

        harvestColumn, sellByColumn = st.columns(2)
        harvestDate = harvestColumn.date_input(
            "Harvest date",
            value=date.today(),
            max_value=date.today()
        )
        sellByDate = sellByColumn.date_input(
            "Sell-by date (optional)",
            value=None
        )
        recordHarvestSubmitted = st.form_submit_button(
            "Save harvested lot",
            type="primary",
            icon=":material/check:",
            width="stretch"
        )
        if sellByDate is not None and sellByDate < harvestDate:
            st.warning(
                "The sell-by date cannot be before the harvest date."
            )

    if recordHarvestSubmitted:
        created, lotID, message = addHarvestLot(
            cycleID=selectedCycleID,
            quantity=actualQuantity,
            unit=actualUnit,
            harvestDate=harvestDate,
            sellByDate=sellByDate,
            userID=userID
        )

        if created:
            st.session_state.farmManagementMessage = (
                "Harvest recorded successfully. "
                "You can add photos of it from the Images page."
            )
            st.rerun()
        else:
            st.warning(message)


def cropCycleCardHtml(cycle, imageName):
    from html import escape

    crop = escape(str(cycle.cropName))
    imageData = showcaseImageData(imageName) if imageName else None
    if imageData:
        photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{crop}">'
    else:
        photo = f'<span class="hl-produce-initial">{escape(str(cycle.cropName)[:1])}</span>'

    expected = pd.to_datetime(cycle.expectedHarvestDate, errors="coerce")
    if pd.isna(expected):
        dayText, monthText, whenText, whenTone = "—", "", "No date set", ""
    else:
        daysLeft = (expected.date() - date.today()).days
        dayText, monthText = str(expected.day), expected.strftime("%b").upper()
        if daysLeft > 1:
            whenText = f"Harvest in {daysLeft} days"
            whenTone = "hl-cycle-when-soon" if daysLeft <= 7 else ""
        elif daysLeft == 1:
            whenText, whenTone = "Harvest tomorrow", "hl-cycle-when-soon"
        elif daysLeft == 0:
            whenText, whenTone = "Harvest today", "hl-cycle-when-soon"
        else:
            whenText = f"{-daysLeft} day{'' if daysLeft == -1 else 's'} overdue"
            whenTone = "hl-cycle-when-late"

    expectedText = escape(formatQuantity(cycle.expectedYield, cycle.expectedUnit))
    return (
        f'<article class="hl-cycle"><div class="hl-cycle-photo">{photo}</div>'
        f'<div class="hl-cycle-body"><div class="hl-alloc-date"><b>{dayText}</b><span>{monthText}</span></div>'
        f'<div class="hl-cycle-main"><strong>{crop}</strong>'
        f'<small>Cycle #{int(cycle.cycleID)} · expecting {expectedText}</small>'
        f'<span class="hl-cycle-when {whenTone}">{whenText}</span></div></div></article>'
    )


def showFarmManagement(current_user):
    st.header("Farm and crop-cycle management")

    if current_user["role"] != "farmer":
        denyAccess(current_user, "Attempted to open farm management")
        return

    userID = int(current_user["userID"])


    if "newHarvestLotID" not in st.session_state:
        st.session_state.newHarvestLotID = None

    # Display one-time messages after refreshing.
    if "farmManagementMessage" in st.session_state:
        st.success(
            st.session_state.farmManagementMessage
        )

        del st.session_state.farmManagementMessage


    farmData = selectData(
        """SELECT f.farmID, f.farmName, f.location
        FROM Farms f, OwnedBy o
        WHERE f.farmID = o.farmID
        AND o.userID = %s
        ORDER BY f.farmName""",
        (userID,)
    )

    # Create a farm if this farmer does not have one.
    if farmData.empty:
        st.info("You do not have a farm registered yet.")

        with st.form("create_farm_form"):
            farmName = st.text_input("Farm name")

            farmLocation = st.text_input(
                "Location or emirate"
            )

            createFarmSubmitted = st.form_submit_button(
                "Create farm"
            )

        if createFarmSubmitted:
            farmName = farmName.strip()
            farmLocation = farmLocation.strip()

            if not farmName or not farmLocation:
                st.warning(
                    "Enter the farm name and location."
                )

            else:
                created, message = createFarmForUser(
                    userID,
                    farmName,
                    farmLocation
                )

                if created:
                    st.session_state.farmManagementMessage = (
                        message
                    )

                    st.rerun()

        return

    farmOptions = {
        f"{farm.farmName} · {farm.location}": int(farm.farmID)
        for farm in farmData.itertuples(index=False)
    }
    if len(farmOptions) > 1:
        selectedFarmLabel = st.selectbox(
            "Manage farm",
            list(farmOptions.keys()),
            key="fm_farm"
        )
    else:
        selectedFarmLabel = next(iter(farmOptions))
    farmID = int(farmOptions[selectedFarmLabel])
    selectedFarm = farmData[farmData["farmID"] == farmID].iloc[0]
    farmName = str(selectedFarm["farmName"])
    farmLocation = str(selectedFarm["location"])

    from html import escape
    farmStats = selectData(
        """SELECT
               (SELECT COUNT(*) FROM CropCycle WHERE farmID = %s AND status = 1) AS activeCycles,
               (SELECT COUNT(*) FROM HarvestLots hl, CropCycle cc
                WHERE hl.cycleID = cc.cycleID AND cc.farmID = %s AND hl.status = 1) AS activeLots,
               (SELECT MIN(expectedHarvestDate) FROM CropCycle
                WHERE farmID = %s AND status = 1 AND expectedHarvestDate >= %s) AS nextHarvest""",
        (farmID, farmID, farmID, date.today()),
        show_error=False
    )
    statsHtml = ""
    if not farmStats.empty:
        stats = farmStats.iloc[0]
        activeCycles = int(stats["activeCycles"])
        activeLots = int(stats["activeLots"])
        nextHarvest = readableDate(stats["nextHarvest"])
        statsHtml = (
            '<span><span class="material-symbols-rounded" aria-hidden="true">potted_plant</span>'
            f'{activeCycles} active crop cycle{"" if activeCycles == 1 else "s"}</span>'
            '<span><span class="material-symbols-rounded" aria-hidden="true">inventory_2</span>'
            f'{activeLots} active lot{"" if activeLots == 1 else "s"}</span>'
            '<span><span class="material-symbols-rounded" aria-hidden="true">event</span>'
            f'{"Next harvest " + nextHarvest if nextHarvest else "No upcoming harvest"}</span>'
        )

    head = st.container(horizontal=True, vertical_alignment="center", key="fm_farm_head")
    head.markdown(
        '<div class="hl-farm-head"><span class="hl-record-kicker">YOUR FARM</span>'
        f'<strong>{escape(farmName)}</strong><div class="hl-farm-meta">'
        '<span><span class="material-symbols-rounded" aria-hidden="true">location_on</span>'
        f'{escape(farmLocation)}</span>{statsHtml}</div></div>',
        unsafe_allow_html=True
    )

    # Edit the farmer's farm information.
    with head.popover("Edit farm", icon=":material/edit:"):
        with st.form("edit_farm_form"):
            updatedFarmName = st.text_input(
                "Farm name",
                value=farmName
            )

            updatedFarmLocation = st.text_input(
                "Location or emirate",
                value=farmLocation
            )

            updateFarmSubmitted = st.form_submit_button(
                "Save farm changes"
            )

        if updateFarmSubmitted:
            updatedFarmName = updatedFarmName.strip()
            updatedFarmLocation = (
                updatedFarmLocation.strip()
            )

            if (
                not updatedFarmName
                or not updatedFarmLocation
            ):
                st.warning(
                    "Enter the farm name and location."
                )

            else:
                updated = changeData(
                    """UPDATE Farms
                       SET farmName = %s,
                           location = %s
                       WHERE farmID = %s
                       AND EXISTS (
                           SELECT 1 FROM OwnedBy o
                           WHERE o.farmID = Farms.farmID
                           AND o.userID = %s
                       )""",
                    (
                        updatedFarmName,
                        updatedFarmLocation,
                        farmID,
                        userID
                    )
                )

                if updated:
                    st.session_state.farmManagementMessage = (
                        "Farm information updated successfully."
                    )

                    st.rerun()

    with head.popover("Add farm", icon=":material/add:"):
        with st.form("add_another_farm_form"):
            newFarmName = st.text_input("Farm name")
            newFarmLocation = st.text_input("Location or emirate")
            addFarmSubmitted = st.form_submit_button("Add farm")

        if addFarmSubmitted:
            created, message = createFarmForUser(
                userID,
                newFarmName,
                newFarmLocation
            )

            if created:
                st.session_state.farmManagementMessage = message
                st.rerun()
            else:
                st.warning(message)

    crops = selectData(
        """SELECT cropID, cropName
           FROM Crops
           WHERE isActive = 1
           ORDER BY cropName"""
    )

    if crops.empty:
        st.warning(
            "An administrator must add crop names first."
        )
        return

    units = getActiveUnits()

    if not units:
        st.warning("No supported units are configured in the application.")
        return

    cropIDs = crops["cropID"].astype(int).tolist()

    def showCrop(cropID):
        crop = crops[
            crops["cropID"] == cropID
        ].iloc[0]

        return str(crop["cropName"])

    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
        key="fm_cycles_head"
    ):
        st.subheader("Active crop cycles", anchor=False)
        if not st.session_state.get("fm_add_open"):
            st.button(
                "Add crop cycle",
                type="primary",
                icon=":material/add:",
                key="fm_add_open_button",
                on_click=setAddCycleOpen,
                args=(True,)
            )

    # Add a new crop cycle.
    if st.session_state.get("fm_add_open"):
        with st.container(key="fm_add_panel"):
            st.markdown(
                '<div class="hl-record-head"><span class="hl-record-kicker">NEW CROP CYCLE</span>'
                '<strong>Add a crop cycle</strong>'
                f'<span>What you are growing at {escape(farmName)} and when you expect to harvest it.</span></div>',
                unsafe_allow_html=True
            )
            with st.form("add_cycle_form", border=False):
                selectedCropID = st.selectbox(
                    "Crop",
                    cropIDs,
                    format_func=showCrop
                )

                yieldColumn, unitColumn = st.columns(2)
                expectedYield = yieldColumn.number_input(
                    "Expected quantity",
                    min_value=0.01,
                    value=0.01
                )

                expectedUnit = unitColumn.selectbox(
                    "Expected unit",
                    units,
                    index=units.index("kg") if "kg" in units else 0
                )

                expectedHarvestDate = st.date_input(
                    "Expected harvest date",
                    value=date.today(),
                    min_value=date.today()
                )

                addCycleSubmitted = st.form_submit_button(
                    "Add crop cycle",
                    type="primary",
                    icon=":material/check:"
                )

            st.button(
                "Cancel",
                key="fm_add_cancel",
                on_click=setAddCycleOpen,
                args=(False,)
            )

            if addCycleSubmitted:
                created = changeData("INSERT INTO CropCycle(farmID, cropID, expectedUnit, expectedYield, expectedHarvestDate, status) VALUES (%s, %s, %s, %s, %s, 1)",
                    (
                        farmID,
                        selectedCropID,
                        expectedUnit,
                        expectedYield,
                        expectedHarvestDate
                    )
                )

                if created:
                    st.session_state.farmManagementMessage = (
                        "Crop cycle added successfully."
                    )
                    st.session_state.fm_add_open = False

                    st.rerun()

    cycles = selectData("""SELECT cc.cycleID, cc.cropID, c.cropName,cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
       FROM CropCycle cc, Crops c
       WHERE cc.cropID = c.cropID AND cc.farmID = %s AND cc.status = 1
       ORDER BY cc.expectedHarvestDate""",(farmID,))

    cycleHistory = selectData(
        """SELECT cc.cycleID, c.cropName, cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate,
                  COUNT(hl.lotID) AS lotCount,
                  COUNT(DISTINCT hl.unit) AS unitCount,
                  MIN(hl.unit) AS lotUnit,
                  SUM(hl.quantityHarvested) AS harvestedTotal
           FROM CropCycle cc
           JOIN Crops c ON cc.cropID = c.cropID
           LEFT JOIN HarvestLots hl ON hl.cycleID = cc.cycleID
           WHERE cc.farmID = %s AND cc.status = 0
           GROUP BY cc.cycleID, c.cropName, cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
           ORDER BY cc.expectedHarvestDate DESC""",
        (farmID,)
    )

    def showCycleHistory():
        st.subheader(
            f"Finished crop cycles ({len(cycleHistory)})",
            anchor=False
        )
        if cycleHistory.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'history</span><span>No finished crop cycles for this farm yet.</span></div>',
                unsafe_allow_html=True
            )
        else:
            rows = "".join(
                cycleHistoryRowHtml(row)
                for row in cycleHistory.itertuples(index=False)
            )
            st.markdown(f'<div class="hl-alloc-list">{rows}</div>', unsafe_allow_html=True)

    if cycles.empty:
        st.info("No crop cycles have been entered.")
        showCycleHistory()
        return

    cycleImages = selectData(
        """SELECT c.cropName, hi.imageName
           FROM HarvestLotImages hi
           JOIN HarvestLots hl ON hi.lotID = hl.lotID
           JOIN CropCycle cc ON hl.cycleID = cc.cycleID
           JOIN Crops c ON cc.cropID = c.cropID
           LEFT JOIN OwnedBy o ON o.farmID = cc.farmID AND o.userID = %s
           ORDER BY (o.userID IS NULL), hl.harvestDate DESC, hi.imageNumber""",
        (userID,),
        show_error=False
    )
    cropCovers = {} if cycleImages.empty else (
        cycleImages.drop_duplicates("cropName").set_index("cropName")["imageName"].to_dict()
    )

    for cardStart in range(0, len(cycles), 2):
        columns = st.columns(2)
        for column, cycle in zip(columns, cycles.iloc[cardStart:cardStart + 2].itertuples(index=False)):
            cycleID = int(cycle.cycleID)
            with column, st.container(key=f"fm_cyc_card_{cycleID}"):
                st.markdown(
                    cropCycleCardHtml(cycle, cropCovers.get(str(cycle.cropName))),
                    unsafe_allow_html=True
                )
                with st.container(horizontal=True, gap="small", key=f"fm_cyc_actions_{cycleID}"):
                    if st.button(
                        "Record harvest",
                        key=f"record_cycle_{cycleID}",
                        type="primary",
                        icon=":material/add:",
                        width="stretch"
                    ):
                        recordCycleHarvestDialog(cycle, units, userID)

                    if st.button(
                        "",
                        key=f"fm_edit_{cycleID}",
                        icon=":material/edit:",
                        help="Edit crop cycle"
                    ):
                        editCropCycleDialog(cycle, farmID, units)

                    with st.popover("Finish", icon=":material/task_alt:"):
                        st.markdown(
                            '<div class="hl-release-head"><strong>Finish this crop cycle?</strong>'
                            f'<span>{cycle.cropName} · Cycle #{cycleID} moves to crop-cycle history '
                            'and no longer appears when you record a harvest.</span></div>',
                            unsafe_allow_html=True
                        )
                        if st.button(
                            "Yes, finish cycle",
                            key=f"finish_cycle_{cycleID}",
                            type="primary",
                            width="stretch"
                        ):
                            finished, message = finishCropCycle(
                                cycleID,
                                userID
                            )

                            if finished:
                                st.session_state.farmManagementMessage = message
                                st.rerun()
                            else:
                                st.warning(message)

    # Update an existing crop-cycle forecast.
    showCycleHistory()

def setHarvestLotActive(lotID, isActive, userID):
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute("SELECT cycleID FROM HarvestLots WHERE lotID = %s FOR UPDATE", (lotID,))
        lot = cursor.fetchone()
        if lot is None:
            connection.rollback()
            return False, "This harvest lot was not found."
        cursor.execute(
            """SELECT 1 FROM CropCycle cc, OwnedBy o
               WHERE cc.farmID = o.farmID AND cc.cycleID = %s AND o.userID = %s""",
            (lot[0], userID),
        )
        if cursor.fetchone() is None:
            connection.rollback()
            return False, "This lot does not belong to your farm."
        cursor.execute("UPDATE HarvestLots SET status = %s WHERE lotID = %s",
                       (1 if isActive else 0, lotID))
        connection.commit()
        return True, "Lot status updated."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Lot status error:", repr(error))
        return False, "The lot status could not be updated."
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


HARVEST_LOT_STATUS = {
    "available": ("Available", "hl-lot-tag-good"),
    "no available stock": ("No stock left", "hl-lot-tag-off"),
    "past sell-by date": ("Past sell-by", "hl-produce-tag-warn"),
    "inactive": ("Inactive", "hl-lot-tag-off"),
    "calculation unavailable": ("Check stock", "hl-produce-tag-warn"),
}


def readableDate(value):
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else f"{parsed.day} {parsed.strftime('%b %Y')}"


def harvestLotPhotos(farmID):
    return selectData(
        """SELECT hli.lotID, hli.imageName
           FROM HarvestLotImages hli, HarvestLots hl, CropCycle cc
           WHERE hli.lotID = hl.lotID AND hl.cycleID = cc.cycleID AND cc.farmID = %s
           ORDER BY hli.lotID, hli.imageNumber""",
        (farmID,),
        show_error=False
    )


def harvestLotCardHtml(lot, imageData, photoCount):
    from html import escape

    crop = escape(str(lot.cropName))
    unit = str(lot.unit)
    label, tone = HARVEST_LOT_STATUS.get(
        str(lot.saleStatus), (str(lot.saleStatus).capitalize(), "")
    )

    if imageData:
        photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{crop}">'
    else:
        photo = f'<span class="hl-produce-initial">{escape(str(lot.cropName)[:1])}</span>'
    countClass = " hl-gallery-count-none" if photoCount == 0 else ""
    countText = "No photos" if photoCount == 0 else f'{photoCount} photo{"" if photoCount == 1 else "s"}'

    onHandText = "" if pd.isna(lot.stockOnHand) else f"{formatQuantity(lot.stockOnHand, unit)} on hand · "
    if pd.isna(lot.stockOnHand):
        stockHtml = '<div class="hl-produce-available">Stock calculation unavailable</div>'
    else:
        stock = float(lot.stockOnHand)
        reserved = float(lot.reservedQuantity)
        available = float(lot.availableQuantity)
        availablePercent = max(0, min(100, available / stock * 100 if stock > 0 else 0))
        reservedPercent = max(0, min(100 - availablePercent, reserved / stock * 100 if stock > 0 else 0))
        stockHtml = (
            f'<div class="hl-produce-available"><b>{escape(formatQuantity(available, unit))}</b> available</div>'
            f'<div class="hl-produce-bar" title="Available vs reserved">'
            f'<span class="hl-bar-available" style="width:{availablePercent:.1f}%"></span>'
            f'<span class="hl-bar-reserved" style="width:{reservedPercent:.1f}%"></span></div>'
            f'<div class="hl-produce-legend"><span><i class="hl-dot-available"></i>{escape(formatQuantity(available, unit))} free</span>'
            f'<span><i class="hl-dot-reserved"></i>{escape(formatQuantity(reserved, unit))} reserved</span></div>'
        )

    sellBy = pd.to_datetime(lot.sellByDate, errors="coerce")
    if pd.isna(sellBy):
        sellText = "No sell-by date"
    else:
        sellText = f"Sell by {sellBy.day} {sellBy.strftime('%b %Y')}"
        if int(lot.status) == 1:
            daysLeft = (sellBy.date() - date.today()).days
            if daysLeft < 0:
                sellText += f" · {-daysLeft} day{'' if daysLeft == -1 else 's'} past"
            elif daysLeft == 0:
                sellText += " · last day"
            else:
                sellText += f" · {daysLeft} day{'' if daysLeft == 1 else 's'} left"

    return (
        f'<article class="hl-produce hl-lot"><div class="hl-produce-photo">{photo}'
        f'<span class="hl-produce-tag {tone}">{escape(label)}</span>'
        f'<span class="hl-gallery-count hl-lot-count{countClass}">{countText}</span></div>'
        f'<div class="hl-produce-body"><div class="hl-produce-head"><span class="hl-produce-name">{crop}</span>'
        f'<span class="hl-produce-meta">Lot #{int(lot.lotID)} · harvested {readableDate(lot.harvestDate) or "—"}</span></div>'
        f'{stockHtml}<div class="hl-produce-foot hl-lot-foot"><span>{escape(sellText)}</span>'
        f'<span>{escape(onHandText)}Harvested {escape(formatQuantity(lot.quantityHarvested, unit))}</span></div></div></article>'
    )


def keepHarvestView():
    if st.session_state.get("hv_view") is None:
        st.session_state["hv_view"] = st.session_state.get("hv_view_last", "Cards")
    st.session_state["hv_view_last"] = st.session_state["hv_view"]


def openLotPhotos(lotID):
    goToPage("Images")
    st.session_state["pendingImageLotID"] = int(lotID)


def showHarvestLotMenu(lot, userID):
    lotID = int(lot.lotID)
    isActive = int(lot.status) == 1

    with st.popover("", icon=":material/more_vert:", help="Lot actions"):
        st.markdown(
            f'<div class="hl-release-head"><strong>{lot.cropName} · Lot #{lotID}</strong>'
            f'<span>{"Active" if isActive else "Inactive"} lot</span></div>',
            unsafe_allow_html=True
        )
        if isActive and st.button(
            "Record loss or correction",
            key=f"hv_adjust_{lotID}",
            icon=":material/edit_note:",
            width="stretch"
        ):
            lotAdjustmentDialog(lot, userID)

        if st.button(
            "Deactivate lot" if isActive else "Activate lot",
            key=f"hv_toggle_{lotID}",
            icon=":material/visibility_off:" if isActive else ":material/visibility:",
            width="stretch"
        ):
            changed, message = setHarvestLotActive(lotID, not isActive, userID)
            if changed:
                st.session_state.stockMovementMessage = (
                    f"Lot #{lotID} is now {'inactive' if isActive else 'active'}."
                )
                st.rerun()
            else:
                st.warning(message)
        st.caption(
            "Inactive lots move to History and can't be reserved for market requests."
            if isActive else "Activating moves this lot back to Active."
        )


@st.dialog("Record a loss or correction", width="medium")
def lotAdjustmentDialog(lot, userID):
    adjustmentVersion = st.session_state.stockAdjustmentVersion
    selectedLotID = int(lot.lotID)
    lotUnit = str(lot.unit)
    availableQuantity = float(lot.availableQuantity)
    keySuffix = f"{selectedLotID}_{adjustmentVersion}"

    st.markdown(
        f'<div class="hl-release-head"><strong>{lot.cropName} · Lot #{selectedLotID}</strong>'
        f'<span>{formatQuantity(availableQuantity, lotUnit)} available. Losses and removals '
        f'cannot be more than that.</span></div>',
        unsafe_allow_html=True
    )
    movementType = st.segmented_control(
        "Movement type",
        ["loss", "correction"],
        default="loss",
        format_func=lambda value: "Loss" if value == "loss" else "Approved correction",
        key=f"adjustment_type_{keySuffix}"
    ) or "loss"

    correctionConfirmed = True
    correctionEffect = None

    if movementType == "correction":
        correctionEffect = st.radio(
            "Correction effect",
            ["Add stock", "Remove stock"],
            horizontal=True,
            key=f"correction_effect_{keySuffix}"
        )
        correctionConfirmed = st.checkbox(
            "I confirm that this correction has been approved",
            key=f"correction_confirmed_{keySuffix}"
        )

    enteredQuantity = st.number_input(
        f"Quantity ({lotUnit})",
        min_value=0.01,
        value=0.01,
        step=0.01,
        key=f"adjustment_quantity_{keySuffix}"
    )

    if movementType == "loss":
        storedMovementType = "loss"
        movementQuantity = enteredQuantity
        quantityRemoved = enteredQuantity
        reasonLabel = "Reason for the loss (optional)"
    elif correctionEffect == "Add stock":
        storedMovementType = "correction_added"
        movementQuantity = enteredQuantity
        quantityRemoved = 0
        reasonLabel = "Reason for the approved correction"
    else:
        storedMovementType = "correction_removed"
        movementQuantity = enteredQuantity
        quantityRemoved = enteredQuantity
        reasonLabel = "Reason for the approved correction"

    reason = st.text_area(
        reasonLabel,
        key=f"adjustment_reason_{keySuffix}"
    )

    submitted = st.button(
        "Record stock movement",
        type="primary",
        icon=":material/check:",
        width="stretch",
        key=f"record_adjustment_{keySuffix}"
    )

    if submitted:
        if reason.strip():
            savedReason = reason.strip()
        else:
            savedReason = None

        if (
            movementType == "correction"
            and not correctionConfirmed
        ):
            st.warning(
                "Confirm that the correction has been approved."
            )
        elif (
            movementType == "correction"
            and savedReason is None
        ):
            st.warning(
                "Enter the reason for the approved correction."
            )
        elif quantityRemoved > availableQuantity:
            st.warning(
                f"Only {availableQuantity:,.2f} "
                f"{lotUnit} is currently available."
            )
        else:
            recorded, adjustmentMessage = recordStockAdjustment(
                selectedLotID, storedMovementType, movementQuantity,
                lotUnit, savedReason, userID
            )
            if not recorded:
                st.warning(adjustmentMessage)
            if recorded:
                logActivity(
                    userID,
                    "stock_movement_recorded",
                    f"Recorded {storedMovementType} for lot {selectedLotID}"
                )
                if storedMovementType == "loss":
                    message = (
                        f"Loss recorded successfully. "
                        f"{movementQuantity:,.2f} "
                        f"{lotUnit} was removed from stock."
                    )
                elif storedMovementType == "correction_added":
                    message = (
                        f"Approved correction recorded successfully. "
                        f"{movementQuantity:,.2f} "
                        f"{lotUnit} was added to stock."
                    )
                else:
                    message = (
                        f"Approved correction recorded successfully. "
                        f"{movementQuantity:,.2f} "
                        f"{lotUnit} was removed from stock."
                    )

                st.session_state.stockMovementMessage = message

                # New widget keys cause all adjustment inputs to reset.
                st.session_state.stockAdjustmentVersion += 1

                st.rerun()


def showHarvestLotCards(lots, photos, withButtons, userID):
    covers = {} if photos.empty else photos.drop_duplicates("lotID").set_index("lotID")["imageName"].to_dict()
    counts = {} if photos.empty else photos.groupby("lotID").size().to_dict()

    def cardFor(lot):
        lotID = int(lot.lotID)
        imageData = showcaseImageData(covers[lotID]) if lotID in covers else None
        return harvestLotCardHtml(lot, imageData, int(counts.get(lotID, 0)))

    if not withButtons:
        cards = "".join(cardFor(lot) for lot in lots.itertuples(index=False))
        st.markdown(f'<div class="hl-produce-grid">{cards}</div>', unsafe_allow_html=True)
        return

    for start in range(0, len(lots), 4):
        columns = st.columns(4)
        for column, lot in zip(columns, lots.iloc[start:start + 4].itertuples(index=False)):
            lotID = int(lot.lotID)
            with column, st.container(key=f"hv_card_{lotID}"):
                st.markdown(cardFor(lot), unsafe_allow_html=True)
                with st.container(horizontal=True, gap="small", key=f"hv_actions_{lotID}"):
                    st.button(
                        "Manage photos",
                        key=f"hv_photos_{lotID}",
                        icon=":material/photo_library:",
                        on_click=openLotPhotos,
                        args=(lotID,),
                        width="stretch"
                    )
                    showHarvestLotMenu(lot, userID)


def setHarvestRecordOpen(isOpen):
    st.session_state["hv_record_open"] = isOpen


def goToRecordHarvest():
    goToPage("Harvest management")
    st.session_state["hv_record_open"] = True


def showHarvestManagement(current_user):
    st.header("Harvest and stock management")
    if "imageUploadMessage" in st.session_state:
        st.success(
            st.session_state.imageUploadMessage
        )

        del st.session_state.imageUploadMessage

    if "imageUploadWarning" in st.session_state:
        st.warning(
            st.session_state.imageUploadWarning
        )

        del st.session_state.imageUploadWarning
    if "stockAdjustmentVersion" not in st.session_state:
        st.session_state.stockAdjustmentVersion = 0

    if "stockMovementMessage" in st.session_state:
        st.success(
            st.session_state.stockMovementMessage
        )

        del st.session_state.stockMovementMessage
    if "selectedImageLotID" not in st.session_state:
        st.session_state.selectedImageLotID = None
    if "stockMovementMessage" in st.session_state:
        st.success(
            st.session_state.stockMovementMessage
        )

        del st.session_state.stockMovementMessage

    if current_user["role"] != "farmer":
        denyAccess(current_user, "Attempted to open harvest management")
        return
    
    userID = int(current_user["userID"])
    units = getActiveUnits()

    if not units:
        st.warning("No supported units are configured in the application.")
        return

    # Get the farmer's farm.
    farmData = selectData(
        """SELECT f.farmID, f.farmName, f.location
        FROM Farms f, OwnedBy o
        WHERE f.farmID = o.farmID
        AND o.userID = %s
        ORDER BY f.farmName""",
        (userID,)
    )
    if farmData.empty:
        st.info("Create your farm before recording harvests.")
        return

    farmOptions = {
        f"{farm.farmName} · {farm.location}": int(farm.farmID)
        for farm in farmData.itertuples(index=False)
    }
    if len(farmOptions) > 1:
        selectedFarmLabel = st.selectbox(
            "Farm",
            list(farmOptions.keys()),
            key="hv_farm"
        )
    else:
        selectedFarmLabel = next(iter(farmOptions))
    selectedFarmID = int(farmOptions[selectedFarmLabel])
    selectedFarm = farmData[farmData["farmID"] == selectedFarmID].iloc[0]
    farmName = str(selectedFarm["farmName"])
    farmLocation = str(selectedFarm["location"])


    # Get only crop cycles belonging to this farmer.
    cycles = selectData( """SELECT cc.cycleID, c.cropName, cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
           FROM CropCycle cc, Crops c
           WHERE cc.cropID = c.cropID AND cc.farmID = %s AND cc.status = 1
             AND c.isActive = 1
           ORDER BY cc.expectedHarvestDate""", (selectedFarmID,))

    from html import escape
    growingText = f"{len(cycles)} crop cycle{'' if len(cycles) == 1 else 's'} growing"
    with st.container(horizontal=True, vertical_alignment="center", key="hv_farm_head"):
        st.markdown(
            '<div class="hl-farm-head"><span class="hl-record-kicker">YOUR FARM</span>'
            f'<strong>{escape(farmName)}</strong><div class="hl-farm-meta">'
            '<span><span class="material-symbols-rounded" aria-hidden="true">location_on</span>'
            f'{escape(farmLocation)}</span>'
            '<span><span class="material-symbols-rounded" aria-hidden="true">potted_plant</span>'
            f'{growingText}</span></div></div>',
            unsafe_allow_html=True
        )
        if not cycles.empty and not st.session_state.get("hv_record_open"):
            st.button(
                "Record harvest",
                icon=":material/add:",
                type="primary",
                key="hv_record_toggle",
                on_click=setHarvestRecordOpen,
                args=(True,)
            )

    if cycles.empty:
        st.info("Add a crop cycle before recording a harvest.")

    else:
        cycleIDs = cycles["cycleID"].astype(int).tolist()

        def showCycle(cycleID):
            cycle = cycles[
                cycles["cycleID"] == cycleID
            ].iloc[0]

            expectedDate = readableDate(cycle["expectedHarvestDate"]) or "no date set"
            return (
                f"{cycle['cropName']} · Cycle #{int(cycleID)} · expecting "
                f"{formatQuantity(cycle['expectedYield'], cycle['expectedUnit'])} on {expectedDate}"
            )

        if st.session_state.get("hv_record_open"):
            with st.container(key="hv_record_panel"):
                st.markdown(
                    '<div class="hl-record-head"><span class="hl-record-kicker">RECORD A HARVEST</span>'
                    '<strong>New harvest lot</strong>'
                    '<span>Pick the crop cycle, then enter what you harvested.</span></div>',
                    unsafe_allow_html=True
                )
                selectedCycleID = st.selectbox(
                    "Crop cycle",
                    cycleIDs,
                    format_func=showCycle,
                    key="harvest_cycle"
                )

                selectedCycle = cycles[
                    cycles["cycleID"] == selectedCycleID
                ].iloc[0]

                expectedUnit = str(
                    selectedCycle["expectedUnit"]
                )

                if expectedUnit in units:
                    defaultUnitIndex = units.index(expectedUnit)
                else:
                    defaultUnitIndex = 0

                quantityCol, unitCol = st.columns([2, 1])
                with quantityCol:
                    quantityHarvested = st.number_input(
                        "Quantity harvested",
                        min_value=0.01,
                        value=0.01,
                        step=0.01,
                        key="harvest_quantity"
                    )
                with unitCol:
                    unit = st.selectbox(
                        "Unit",
                        units,
                        index=defaultUnitIndex,
                        key="harvest_unit"
                    )

                if unit != expectedUnit:
                    st.markdown(
                        '<div class="hl-alloc-note"><span class="material-symbols-rounded" aria-hidden="true">'
                        f'swap_horiz</span><span>This crop cycle expects <b>{expectedUnit}</b>. A lot saved in '
                        f'<b>{unit}</b> only matches market requests in {unit}, because stock is never '
                        'converted between units.</span></div>',
                        unsafe_allow_html=True
                    )

                dateCol, sellByCol = st.columns(2)
                with dateCol:
                    harvestDate = st.date_input(
                        "Harvest date",
                        value=date.today(),
                        max_value=date.today(),
                        key="harvest_date"
                    )
                with sellByCol:
                    sellByDate = st.date_input(
                        "Sell-by date (optional)",
                        value=None,
                        key="sell_by_date"
                    )

                with st.container(horizontal=True, gap="small"):
                    recordHarvestSubmitted = st.button(
                        "Save harvest",
                        type="primary",
                        icon=":material/check:",
                        key="record_harvest"
                    )
                    st.button(
                        "Cancel",
                        key="hv_record_cancel",
                        on_click=setHarvestRecordOpen,
                        args=(False,)
                    )

                if recordHarvestSubmitted:
                    if sellByDate is not None and sellByDate < harvestDate:
                        st.warning(
                            "The sell-by date cannot be before "
                            "the harvest date."
                        )

                    else:
                        created, lotID, message = addHarvestLot(
                            cycleID=selectedCycleID,
                            quantity=quantityHarvested,
                            unit=unit,
                            harvestDate=harvestDate,
                            sellByDate=sellByDate,
                            userID=userID
                        )

                        if created:
                            st.session_state.stockMovementMessage = message
                            st.session_state.hv_record_open = False
                            st.rerun()
                        else:
                            st.warning(message)


    inventory = selectData( """SELECT hl.lotID, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate, hl.status
           FROM HarvestLots hl, CropCycle cc, Crops c
           WHERE hl.cycleID = cc.cycleID AND cc.cropID = c.cropID AND cc.farmID = %s
           ORDER BY hl.harvestDate DESC, c.cropName""",(selectedFarmID,))

    st.subheader("My harvested inventory")

    if inventory.empty:
        st.info("No harvest lots have been recorded.")
        return

    inventory["stockOnHand"] = 0.0
    inventory["reservedQuantity"] = 0.0
    inventory["availableQuantity"] = 0.0
    inventory["saleStatus"] = ""

    for index, lot in inventory.iterrows():
        lotID = int(lot["lotID"])

        (
            stockOnHand,
            reservedQuantity,
            availableQuantity
        ) = calculateLotAvailability(lotID)

        # Do not attempt float(None).
        if (
            stockOnHand is None
            or reservedQuantity is None
            or availableQuantity is None
        ):
            inventory.at[index, "stockOnHand"] = float("nan")
            inventory.at[index, "reservedQuantity"] = float("nan")
            inventory.at[index, "availableQuantity"] = float("nan")
            inventory.at[index, "saleStatus"] = (
                "calculation unavailable"
            )

            st.warning(
                f"The stock calculation for "
                f"{lot['cropName']} could not be completed."
            )

            continue

        inventory.at[index, "stockOnHand"] = float(
            stockOnHand
        )

        inventory.at[index, "reservedQuantity"] = float(
            reservedQuantity
        )

        inventory.at[index, "availableQuantity"] = float(
            availableQuantity
        )

        if pd.isna(lot["sellByDate"]):
            sellByDateValue = None
        else:
            sellByDateValue = pd.to_datetime(
                lot["sellByDate"]
            ).date()

        # Keep the saved lot status separate from how much stock is left.

        if sellByDateValue is not None and sellByDateValue < date.today():
            inventory.at[
                index,
                "saleStatus"
            ] = "past sell-by date"

        elif int(lot["status"]) == 0:
            inventory.at[index, "saleStatus"] = "inactive"

        elif float(availableQuantity) <= 0:
            inventory.at[
                index,
                "saleStatus"
            ] = "no available stock"

        else:
            inventory.at[
                index,
                "saleStatus"
            ] = "available"

    activeInventory = inventory[inventory["status"] == 1].copy()
    historyInventory = inventory[inventory["status"] == 0].copy()

    tableColumns = ["cropName", "quantityHarvested", "unit", "harvestDate",
                    "sellByDate", "stockOnHand", "reservedQuantity",
                    "availableQuantity", "saleStatus"]
    tableLabels = ["Crop", "Quantity harvested", "Unit", "Harvest date",
                   "Sell-by date", "Stock on hand", "Reserved", "Available",
                   "Status"]
    photos = harvestLotPhotos(selectedFarmID)

    with st.container(horizontal=True, horizontal_alignment="right"):
        view = st.segmented_control(
            "Show as",
            ["Cards", "Table"],
            default="Cards",
            key="hv_view",
            on_change=keepHarvestView,
            label_visibility="collapsed"
        )

    activeTab, historyTab = st.tabs(
        [f"Active ({len(activeInventory)})", f"History ({len(historyInventory)})"]
    )

    for tab, lots, emptyText, withButtons in [
        (historyTab, historyInventory, "There are no completed harvest lots for this farm.", True),
        (activeTab, activeInventory, "There are no active harvest lots for this farm.", True),
    ]:
        with tab:
            if lots.empty:
                st.markdown(
                    '<div class="hl-gallery-empty"><span class="material-symbols-rounded" '
                    f'aria-hidden="true">inventory_2</span>{emptyText}</div>',
                    unsafe_allow_html=True
                )
            elif view == "Table":
                display = lots[tableColumns].copy()
                display.columns = tableLabels
                showTable(display, use_container_width=True, hide_index=True)
            else:
                showHarvestLotCards(lots, photos, withButtons, userID)


def addHarvestLot(cycleID, quantity,unit, harvestDate,sellByDate,userID):
    connection = None
    cursor = None

    try:
        if quantity <= 0:
            return False, None, (
                "The harvested quantity must be greater than zero."
            )

        unit = str(unit).strip().lower()

        if not isValidUnit(unit):
            return False, None, "Choose a valid harvest unit."

        if harvestDate > date.today():
            return False, None, (
                "The harvest date cannot be in the future."
            )

        if (
            sellByDate is not None
            and sellByDate < harvestDate
        ):
            return (
                False,
                None,
                "The sell-by date cannot be before the harvest date."
            )
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT cc.cycleID
               FROM CropCycle cc, OwnedBy o
               WHERE cc.farmID = o.farmID
               AND cc.cycleID = %s
               AND o.userID = %s
               AND cc.status = 1
               AND EXISTS (SELECT 1 FROM Crops c WHERE c.cropID = cc.cropID AND c.isActive = 1)""",
            (cycleID, userID)
        )

        ownedCycle = cursor.fetchone()

        if ownedCycle is None:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to record a harvest for crop cycle {cycleID}"
            )
            return False, None, (
                "This crop cycle does not belong to your farm."
            )

        movementDate = datetime.combine(
            harvestDate,
            datetime.min.time()
        )

        cursor.execute(
            """INSERT INTO HarvestLots
               (cycleID, quantityHarvested, unit,
                harvestDate, sellByDate, status)
               VALUES (%s, %s, %s, %s, %s, 1)""",
            (
                cycleID,
                quantity,
                unit,
                harvestDate,
                sellByDate
            )
        )

        lotID = cursor.lastrowid

        cursor.execute(
            """INSERT INTO StockMovements
               (lotID, allocationID, movementType,
                quantity, unit, dateOccurred, reason, performedBy)
               VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)""",
            (
                lotID,
                "added",
                quantity,
                unit,
                movementDate,
                "Initial harvested quantity",
                userID
            )
        )


        connection.commit()
        logActivity(userID, "harvest_recorded", f"Recorded harvest lot {lotID}")

        return (
            True,
            lotID,
            "Harvested lot recorded successfully."
        )

    except Exception as error:
        if connection:
            connection.rollback()

        print("Harvest creation error:", error)

        return (
            False,
            None,
            "The harvested lot could not be recorded."
        )

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()

def finishCropCycle(cycleID, userID):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT cc.cycleID
               FROM CropCycle cc, OwnedBy o
               WHERE cc.farmID = o.farmID AND cc.cycleID = %s AND o.userID = %s AND cc.status = 1""", (cycleID, userID)
        )

        cycle = cursor.fetchone()

        if cycle is None:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to finish crop cycle {cycleID}"
            )
            return False, (
                "This crop cycle is unavailable or "
                "does not belong to your farm."
            )

        cursor.execute(
            """SELECT lotID
               FROM HarvestLots
               WHERE cycleID = %s
               LIMIT 1""",
            (cycleID,)
        )

        harvestLot = cursor.fetchone()

        if harvestLot is None:
            return False, (
                "Record at least one harvest lot before "
                "finishing this crop cycle."
            )

        cursor.execute(
            """UPDATE CropCycle
               SET status = 0
               WHERE cycleID = %s""",
            (cycleID,)
        )

        connection.commit()

        logActivity(
            userID,
            "crop_cycle_finished",
            f"Finished crop cycle {cycleID}"
        )

        return True, "Crop cycle moved to history."

    except Exception as error:
        if connection:
            connection.rollback()

        print("Crop-cycle completion error:", error)

        return False, (
            "The crop cycle could not be completed."
        )

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()
            
def farmerMatchingStock(userID):
    lots = selectData(
        """SELECT hl.lotID, cc.cropID, hl.unit
           FROM HarvestLots hl, CropCycle cc, OwnedBy o
           WHERE hl.cycleID = cc.cycleID AND cc.farmID = o.farmID AND o.userID = %s
           AND hl.status = 1 AND hl.harvestDate <= %s
           AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s)""",
        (userID, date.today(), date.today())
    )
    totals = {}
    for lot in lots.itertuples(index=False):
        availableQuantity = calculateLotAvailability(int(lot.lotID))[2]
        if availableQuantity is not None and float(availableQuantity) > 0:
            key = (int(lot.cropID), str(lot.unit))
            totals[key] = totals.get(key, 0.0) + float(availableQuantity)
    return totals


def selectAllocRequest(requestID):
    st.session_state["alloc_request"] = requestID


def showRequestCards(requests, userID, selectedID):
    from html import escape

    cropImages = selectData(
        """SELECT hi.lotID, c.cropName, hi.imageName
           FROM HarvestLotImages hi
           JOIN HarvestLots hl ON hi.lotID = hl.lotID
           JOIN CropCycle cc ON hl.cycleID = cc.cycleID
           JOIN Crops c ON cc.cropID = c.cropID
           LEFT JOIN OwnedBy o ON o.farmID = cc.farmID AND o.userID = %s
           ORDER BY (o.userID IS NULL), hl.harvestDate DESC, hi.imageNumber""",
        (userID,)
    )

    rows = list(requests.itertuples(index=False))
    for start in range(0, len(rows), 4):
        columns = st.columns(4)
        for column, row in zip(columns, rows[start:start + 4]):
            showRequestCard(column, row, cropImages, selectedID)


def showRequestCard(column, row, cropImages, selectedID):
    from html import escape

    cropName = str(row.cropName)
    unit = str(row.unit)
    imageData = produceImageData([], cropName, cropImages)
    if imageData:
        photo = f'<img src="data:image/jpeg;base64,{imageData}" alt="{escape(cropName)}">'
    else:
        photo = f'<span class="hl-produce-initial">{escape(cropName[:1])}</span>'

    requested = float(row.quantityRequested)
    received = float(row.quantityReceived)
    reserved = float(row.outstandingReserved)
    remaining = float(row.quantityRemaining)
    canOffer = float(row.canOffer)
    receivedPercent = max(0, min(100, received / requested * 100 if requested > 0 else 0))
    reservedPercent = max(0, min(100 - receivedPercent, reserved / requested * 100 if requested > 0 else 0))

    if canOffer >= remaining:
        tag = '<span class="hl-produce-tag hl-req-tag-good">You can fill this</span>'
    elif canOffer > 0:
        tag = f'<span class="hl-produce-tag">You can fill {escape(formatQuantity(canOffer, unit))}</span>'
    else:
        tag = '<span class="hl-produce-tag hl-req-tag-none">No matching stock</span>'

    requestDate = pd.to_datetime(row.requestDate, errors="coerce")
    dateText = "—" if pd.isna(requestDate) else f"{requestDate.day} {requestDate.strftime('%b %Y')}"
    statusText = "Part filled" if str(row.status) == "partiallyFulfilled" else "Pending"

    card = (
        f'<article class="hl-produce hl-req"><div class="hl-produce-photo">{photo}{tag}</div>'
        f'<div class="hl-produce-body"><div class="hl-produce-head"><span class="hl-produce-name">{escape(cropName)}</span>'
        f'<span class="hl-produce-meta">Request #{int(row.requestID)} · {statusText}</span></div>'
        f'<div class="hl-produce-available"><b>{escape(formatQuantity(remaining, unit))}</b> still needed '
        f'of {escape(formatQuantity(requested, unit))}</div>'
        f'<div class="hl-produce-bar" title="Received and reserved so far">'
        f'<span class="hl-bar-available" style="width:{receivedPercent:.1f}%"></span>'
        f'<span class="hl-bar-reserved" style="width:{reservedPercent:.1f}%"></span></div>'
        f'<div class="hl-produce-legend"><span><i class="hl-dot-available"></i>{escape(formatQuantity(received, unit))} received</span>'
        f'<span><i class="hl-dot-reserved"></i>{escape(formatQuantity(reserved, unit))} reserved</span></div>'
        f'<div class="hl-produce-foot hl-req-foot">'
        f'<span><span class="material-symbols-rounded" aria-hidden="true">storefront</span>'
        f'{escape(str(row.marketName))} · {escape(str(row.marketLocation))}</span>'
        f'<span><span class="material-symbols-rounded" aria-hidden="true">person</span>'
        f'{escape(str(row.requestedBy))} · {dateText}</span></div></div></article>'
    )

    requestID = int(row.requestID)
    selected = requestID == selectedID
    with column, st.container(key=f"req_card_{requestID}"):
        st.markdown(card.replace('class="hl-produce hl-req"',
            f'class="hl-produce hl-req{" hl-req-selected" if selected else ""}"'), unsafe_allow_html=True)
        st.button(
            "Selected" if selected else ("Allocate stock" if canOffer > 0 else "View request"),
            key=f"req_pick_{requestID}",
            icon=":material/check:" if selected else ":material/inventory:",
            type="primary" if selected else "secondary",
            width="stretch",
            on_click=selectAllocRequest,
            args=(requestID,)
        )


ALLOCATION_STATUS = {
    "reserved": ("Reserved", "hl-alloc-pill"),
    "partiallyDelivered": ("Partly delivered", "hl-alloc-pill hl-alloc-pill-part"),
    "delivered": ("Delivered", "hl-alloc-pill hl-alloc-pill-done"),
    "canceled": ("Cancelled", "hl-alloc-pill hl-alloc-pill-off"),
}


def allocationRowHtml(row):
    from html import escape

    unit = str(row.unit)
    allocated = pd.to_datetime(row.allocationDate, errors="coerce")
    dayText, monthText = ("—", "") if pd.isna(allocated) else (str(allocated.day), allocated.strftime("%b").upper())
    statusText, statusClass = ALLOCATION_STATUS.get(str(row.status), (str(row.status), "hl-alloc-pill"))
    remainingText = ""
    if str(row.status) in ("reserved", "partiallyDelivered"):
        remainingText = f"<small>{escape(formatQuantity(float(row.reservedRemaining), unit))} still reserved</small>"
    return (
        f'<div class="hl-alloc-row"><div class="hl-alloc-date"><b>{dayText}</b><span>{monthText}</span></div>'
        f'<div class="hl-alloc-main"><strong>{escape(str(row.cropName))} · '
        f'{escape(formatQuantity(float(row.quantityAllocated), unit))}</strong>'
        f'<small>{escape(str(row.marketName))} · for {escape(str(row.requestedBy))}</small>'
        f'<small>Allocation #{int(row.allocationID)} · Request #{int(row.requestID)} · Lot #{int(row.lotID)}</small></div>'
        f'<div class="hl-alloc-side"><span class="{statusClass}">{statusText}</span>{remainingText}</div></div>'
    )


def showReleaseControl(row, userID):
    allocationID = int(row.allocationID)
    unit = str(row.unit)
    maximumCancellation = float(row.reservedRemaining)

    with st.popover("Release", icon=":material/undo:"):
        st.markdown(
            f'<div class="hl-release-head"><strong>Release reserved stock</strong>'
            f'<span>{formatQuantity(maximumCancellation, unit)} reserved on lot #{int(row.lotID)}. '
            f'Released stock goes back to available.</span></div>',
            unsafe_allow_html=True
        )
        with st.form(f"release_form_{allocationID}", clear_on_submit=True, border=False):
            cancellationQuantity = st.number_input(
                f"Amount to release ({unit})",
                min_value=0.01,
                max_value=maximumCancellation,
                value=maximumCancellation,
                key=f"release_qty_{allocationID}"
            )
            reason = st.text_input(
                "Reason (optional)",
                key=f"release_reason_{allocationID}"
            )
            cancelSubmitted = st.form_submit_button(
                "Release to stock",
                type="primary",
                width="stretch"
            )
        if cancelSubmitted:
            canceled, message = cancelReservedQuantity(
                allocationID=allocationID,
                cancellationQuantity=cancellationQuantity,
                reason=reason,
                userID=userID
            )
            if canceled:
                st.session_state.allocationMessage = (
                    f"Reservation canceled successfully. "
                    f"{cancellationQuantity:,.2f} "
                    f"{unit} was "
                    f"returned to available stock."
                )
                st.rerun()
            else:
                st.warning(message)


def showAllocationList(allocations, userID):
    activeMask = allocations["status"].isin(["reserved", "partiallyDelivered"])
    active = allocations[activeMask]
    history = allocations[~activeMask]
    activeTab, historyTab = st.tabs([f"Active ({len(active)})", f"History ({len(history)})"])

    with activeTab:
        if active.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'inventory_2</span>There are no active allocations.</div>',
                unsafe_allow_html=True
            )
        else:
            with st.container(key="alloc_active_list", gap=None):
                for row in active.itertuples(index=False):
                    with st.container(
                        key=f"alloc_row_{int(row.allocationID)}",
                        horizontal=True,
                        vertical_alignment="center",
                        gap="small"
                    ):
                        st.markdown(allocationRowHtml(row), unsafe_allow_html=True, width="stretch")
                        if float(row.reservedRemaining) > 0:
                            showReleaseControl(row, userID)

    with historyTab:
        if history.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'inventory_2</span>There are no past allocations.</div>',
                unsafe_allow_html=True
            )
        else:
            rows = "".join(allocationRowHtml(row) for row in history.itertuples(index=False))
            st.markdown(f'<div class="hl-alloc-list">{rows}</div>', unsafe_allow_html=True)


def showFarmerAllocations(current_user):
    st.header("Market requests and allocations")

    if current_user["role"] != "farmer":
        denyAccess(current_user, "Attempted to open farmer allocation management")
        return

    userID = int(current_user["userID"])

    if "allocationMessage" in st.session_state:
        st.success(st.session_state.allocationMessage)
        del st.session_state.allocationMessage

    requests = selectData(
        """SELECT mr.requestID, CONCAT(u.fname, ' ', u.lname) AS requestedBy, m.marketName, m.location AS marketLocation, c.cropName, mr.cropID, mr.quantityRequested, mr.unit, mr.requestDate, mr.reqStatus AS status
        FROM MarketRequests mr, Users u, Markets m, Crops c
        WHERE mr.userID = u.userID AND mr.marketID = m.marketID AND mr.cropID = c.cropID AND mr.reqStatus IN ('pending', 'partiallyFulfilled')
        ORDER BY mr.requestDate"""
    )
    if not requests.empty:
        requests["quantityRequested"] = pd.to_numeric(
            requests["quantityRequested"]
        )

        requests["quantityAllocated"] = 0.0
        requests["quantityReceived"] = 0.0
        requests["outstandingReserved"] = 0.0
        requests["quantityRemaining"] = 0.0

        for index, request in requests.iterrows():
            requestID = int(request["requestID"])

            progress = calculateRequestProgress(requestID)

            if progress is None:
                continue

            requests.at[
                index,
                "quantityAllocated"
            ] = progress["allocatedQuantity"]

            requests.at[
                index,
                "quantityReceived"
            ] = progress["receivedQuantity"]

            requests.at[
                index,
                "outstandingReserved"
            ] = progress["outstandingReserved"]

            requests.at[
                index,
                "quantityRemaining"
            ] = progress["remainingQuantity"]

        requests = requests[
            requests["quantityRemaining"] > 0
        ]

    st.subheader("Open market requests")

    if requests.empty:
        st.info("There are no open market requests.")

    else:
        matchingStock = farmerMatchingStock(userID)
        requests["canOffer"] = [
            matchingStock.get((int(row.cropID), str(row.unit)), 0.0)
            for row in requests.itertuples(index=False)
        ]
        fillable = requests[requests["canOffer"] > 0]

        filterCol, viewCol = st.columns([0.6, 0.4], vertical_alignment="center")
        with filterCol:
            requestFilter = st.segmented_control(
                "Show requests",
                ["mine", "all"],
                format_func=lambda option: (
                    f"Ones I can fill ({len(fillable)})" if option == "mine"
                    else f"All requests ({len(requests)})"
                ),
                default="mine" if len(fillable) else "all",
                key="req_filter",
                label_visibility="collapsed"
            ) or "all"
        with viewCol:
            with st.container(horizontal=True, horizontal_alignment="right"):
                requestView = st.segmented_control(
                    "Request view",
                    ["Cards", "Table"],
                    default="Cards",
                    key="req_view",
                    label_visibility="collapsed"
                ) or "Cards"

        shownRequests = fillable if requestFilter == "mine" else requests

        requestIDs = [int(value) for value in requests["requestID"]]
        if st.session_state.get("alloc_request") not in requestIDs:
            st.session_state["alloc_request"] = (
                int(shownRequests["requestID"].iloc[0]) if not shownRequests.empty else requestIDs[0]
            )

        displayedRequests = requests[
            [
                "requestID",
                "requestedBy",
                "marketName",
                "marketLocation",
                "cropName",
                "quantityRequested",
                "quantityAllocated",
                "quantityReceived",
                "outstandingReserved",
                "quantityRemaining",
                "unit",
                "requestDate",
                "status"
            ]
        ].copy()

        displayedRequests.columns = [
            "Request #",
            "Requested by",
            "Market",
            "Market location",
            "Crop",
            "Requested",
            "Allocated historically",
            "Received",
            "Reserved now",
            "Remaining",
            "Unit",
            "Request date",
            "Status"
        ]

        if shownRequests.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'inventory_2</span>None of the open requests match your available stock right now.</div>',
                unsafe_allow_html=True
            )
        elif requestView == "Table":
            showTable(
                displayedRequests[displayedRequests["Request #"].isin(shownRequests["requestID"])],
                use_container_width=True,
                hide_index=True
            )
        else:
            showRequestCards(shownRequests, userID, st.session_state["alloc_request"])

        from html import escape

        def showRequest(optionID):
            request = requests[requests["requestID"] == optionID].iloc[0]
            return (
                f"{request['cropName']} · Request #{optionID} · {request['marketName']} · "
                f"{formatQuantity(float(request['quantityRemaining']), str(request['unit']))} needed"
            )

        st.selectbox(
            "Request to allocate",
            requestIDs,
            format_func=showRequest,
            key="alloc_request"
        )
        selectedRequest = requests[
            requests["requestID"] == st.session_state["alloc_request"]
        ].iloc[0]
        requestID = int(
            selectedRequest["requestID"]
        )
        cropID = int(
            selectedRequest["cropID"]
        )
        requestUnit = str(
            selectedRequest["unit"]
        )
        requestRemaining = float(
            selectedRequest["quantityRemaining"]
        )
        cropName = str(selectedRequest["cropName"])

        lots = selectData(
            """SELECT hl.lotID, f.farmName, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
               FROM HarvestLots hl, CropCycle cc, Farms f, Crops c, OwnedBy o
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.farmID = o.farmID AND o.userID = %s AND c.cropID = %s AND hl.unit = %s AND hl.status = 1 AND hl.harvestDate <= %s AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s)
               ORDER BY hl.sellByDate, hl.harvestDate""",
            (
                userID,
                cropID,
                requestUnit,
                date.today(),
                date.today()
            ))

        if not lots.empty:
            lots["availableQuantity"] = 0.0
            invalidRows = []

            for index, lot in lots.iterrows():
                lotID = int(lot["lotID"])

                (
                    stockOnHand,
                    reservedQuantity,
                    availableQuantity
                ) = calculateLotAvailability(lotID)

                if availableQuantity is None:
                    invalidRows.append(index)

                else:
                    lots.at[
                        index,
                        "availableQuantity"
                    ] = float(availableQuantity)

            if invalidRows:
                lots = lots.drop(index=invalidRows)

            lots = lots[
                lots["availableQuantity"] > 0
            ]

        with st.container(key="alloc_panel"):
            st.markdown(
                f'<div class="hl-lotpanel-head"><div><span class="hl-lotpanel-eyebrow">Selected request</span>'
                f'<h3>{escape(cropName)}</h3>'
                f'<span class="hl-gallery-meta">Request #{requestID} · {escape(str(selectedRequest["marketName"]))}, '
                f'{escape(str(selectedRequest["marketLocation"]))} · for {escape(str(selectedRequest["requestedBy"]))}</span></div>'
                f'<span class="hl-gallery-count">{escape(formatQuantity(requestRemaining, requestUnit))} needed</span></div>',
                unsafe_allow_html=True
            )

            if lots.empty:
                otherUnits = sorted(
                    unit for (stockCrop, unit) in matchingStock
                    if stockCrop == cropID and unit != requestUnit
                )
                if otherUnits:
                    reason = (
                        f"You have {escape(cropName.lower())} in {escape(', '.join(otherUnits))}, but this request "
                        f"is in {escape(requestUnit)}. Stock can only be allocated in the same unit."
                    )
                else:
                    reason = (
                        f"You have no {escape(cropName.lower())} in stock to allocate. Record a harvest of "
                        f"{escape(cropName.lower())} in {escape(requestUnit)} to fill this request."
                    )
                st.markdown(
                    '<div class="hl-alloc-note"><span class="material-symbols-rounded" aria-hidden="true">'
                    f'info</span>{reason}</div>',
                    unsafe_allow_html=True
                )
            else:
                lotRows = lots.index.tolist()

                def showLot(rowIndex):
                    lot = lots.loc[rowIndex]
                    sellBy = pd.to_datetime(lot["sellByDate"], errors="coerce")
                    sellByText = "" if pd.isna(sellBy) else f" · sell by {sellBy.day} {sellBy.strftime('%b %Y')}"
                    return (
                        f"Lot #{int(lot['lotID'])} · "
                        f"{formatQuantity(float(lot['availableQuantity']), str(lot['unit']))} available"
                        f"{sellByText}"
                    )

                with st.form("allocation_form", border=False):
                    selectedLotRow = st.selectbox(
                        "Lot to reserve from",
                        lotRows,
                        format_func=showLot
                    )
                    selectedLot = lots.loc[
                        selectedLotRow
                    ]
                    maximumAllocation = min(
                        float(
                            selectedLot["availableQuantity"]
                        ),
                        requestRemaining
                    )
                    allocationQuantity = st.number_input(
                        f"Amount to reserve ({requestUnit})",
                        min_value=0.01,
                        max_value=float(maximumAllocation),
                        value=min(
                            1.0,
                            float(maximumAllocation)
                        ),
                        help=f"Up to {formatQuantity(maximumAllocation, requestUnit)}"
                    )
                    allocationSubmitted = (
                        st.form_submit_button(
                            "Reserve stock",
                            type="primary",
                            icon=":material/inventory:"
                        )
                    )

                if allocationSubmitted:
                    created, message = createAllocation(
                        requestID=requestID,
                        lotID=int(selectedLot["lotID"]),
                        quantity=allocationQuantity,
                        userID=userID
                    )
                    if created:
                        st.session_state.allocationMessage = (
                            message
                        )
                        st.rerun()
                    else:
                        st.warning(message)

    allocations = selectData(
        """SELECT a.allocationID, a.requestID, a.lotID, CONCAT(u.fname, ' ', u.lname) AS requestedBy, m.marketName, c.cropName, a.quantityAllocated, hl.unit, a.allocationDate, a.allocStatus AS status
        FROM Allocations a, HarvestLots hl, CropCycle cc, Farms f, OwnedBy o, MarketRequests mr, Users u, Markets m, Crops c
        WHERE a.lotID = hl.lotID AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND f.farmID = o.farmID AND o.userID = %s AND a.requestID = mr.requestID AND mr.userID = u.userID AND mr.marketID = m.marketID AND mr.cropID = c.cropID
        ORDER BY a.allocationDate DESC""",
        (userID,)
    )

    st.subheader("My allocations")

    if allocations.empty:
        st.info("You have not created any allocations.")
        return

    allocations["reservedRemaining"] = 0.0

    # Calculate remaining reserved quantity in Python.
    for index, allocation in allocations.iterrows():
        allocationID = int(
            allocation["allocationID"]
        )

        movements = selectData(
            """SELECT movementType, quantity
               FROM StockMovements
               WHERE allocationID = %s
               ORDER BY dateOccurred""",
            (allocationID,))

        reservedRemaining = 0.0

        if not movements.empty:
            for _, movement in movements.iterrows():
                movementType = str(
                    movement["movementType"]
                ).strip().lower()

                movementQuantity = float(
                    movement["quantity"]
                )

                if movementType == "reserved":
                    reservedRemaining += movementQuantity

                elif movementType == "delivered":
                    reservedRemaining -= movementQuantity

                elif movementType == "canceled":
                    reservedRemaining -= movementQuantity

        allocations.at[
            index,
            "reservedRemaining"
        ] = reservedRemaining

    showAllocationList(allocations, userID)


def createAllocation(requestID, lotID, quantity, userID):
    connection = None
    cursor = None
    try:
        quantity = Decimal(str(quantity))
        if not quantity.is_finite() or quantity <= 0:
            return False, "Allocation quantity must be greater than zero."
        connection = getConnection()
        cursor = connection.cursor()
        # Read the latest committed movements after waiting for a lock.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")

        # Lock the request first, then the lot, so two farmers cannot oversell.
        cursor.execute(
            """SELECT cropID, quantityRequested, unit, reqStatus
               FROM MarketRequests WHERE requestID = %s FOR UPDATE""",
            (requestID,),
        )
        request = cursor.fetchone()
        if request is None:
            connection.rollback()
            return False, "The market request could not be found."
        cropID, requested, requestUnit, requestStatus = request
        if str(requestStatus) not in ("pending", "partiallyFulfilled"):
            connection.rollback()
            return False, "This request is no longer open."

        cursor.execute(
            """SELECT cycleID, unit, harvestDate, sellByDate, status
               FROM HarvestLots WHERE lotID = %s FOR UPDATE""", (lotID,),
        )
        lot = cursor.fetchone()
        if lot is None:
            connection.rollback()
            return False, "The harvested lot was not found."
        cycleID, lotUnit, harvestDate, sellByDate, lotStatus = lot
        cursor.execute(
            """SELECT cc.cropID FROM CropCycle cc, OwnedBy o
               WHERE cc.farmID = o.farmID AND cc.cycleID = %s
               AND o.userID = %s""", (cycleID, userID),
        )
        owned = cursor.fetchone()
        if owned is None:
            connection.rollback()
            return False, "This harvested lot does not belong to your farm."
        if int(owned[0]) != int(cropID) or str(lotUnit) != str(requestUnit):
            connection.rollback()
            return False, "The lot crop or unit does not match this request."
        if int(lotStatus) != 1:
            connection.rollback()
            return False, "This harvest lot is inactive."
        if pd.to_datetime(harvestDate).date() > date.today() or (
            sellByDate is not None and pd.to_datetime(sellByDate).date() < date.today()
        ):
            connection.rollback()
            return False, "This harvest lot is not within its sale dates."
        requested, received, reserved, _ = getLockedRequestProgress(cursor, requestID)
        remainingRequest = requested - received - reserved
        if quantity > remainingRequest:
            connection.rollback()
            return False, f"Only {remainingRequest:,.2f} {requestUnit} remains in this request."
        _, _, available = getLockedLotStock(cursor, lotID)
        if quantity > available:
            connection.rollback()
            return False, f"Only {available:,.2f} {lotUnit} is currently available."

        allocationDate = datetime.now()
        cursor.execute(
            """INSERT INTO Allocations
               (requestID, lotID, allocatedBy, quantityAllocated, allocationDate, allocStatus)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (requestID, lotID, userID, quantity, allocationDate, "reserved"),
        )
        allocationID = cursor.lastrowid
        cursor.execute(
            """INSERT INTO StockMovements
               (lotID, allocationID, movementType, quantity, unit,
                dateOccurred, reason, performedBy)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (lotID, allocationID, "reserved", quantity, lotUnit,
             allocationDate, f"Reserved for market request {requestID}", userID),
        )
        updateRequestStatus(cursor, requestID, "partiallyFulfilled", userID,
                            "Produce reserved by farmer")
        connection.commit()
        logActivity(userID, "allocation_created",
                    f"Created allocation {allocationID} for request {requestID}")
        return True, f"{quantity:,.2f} {lotUnit} was reserved successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Allocation error:", repr(error))
        return False, "The allocation could not be created. Please try again."
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def cancelReservedQuantity(allocationID, cancellationQuantity, reason, userID):
    connection = None
    cursor = None
    try:
        cancellationQuantity = Decimal(str(cancellationQuantity))
        if not cancellationQuantity.is_finite() or cancellationQuantity <= 0:
            return False, "Cancellation quantity must be greater than zero."
        connection = getConnection()
        cursor = connection.cursor()
        # Read the latest committed movements after waiting for a lock.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cursor.execute(
            """SELECT requestID, lotID FROM Allocations
               WHERE allocationID = %s""", (allocationID,),
        )
        ids = cursor.fetchone()
        if ids is None:
            connection.rollback()
            return False, "The allocation could not be found."
        requestID, lotID = int(ids[0]), int(ids[1])

        # Always lock request, lot, then allocation in the same order.
        cursor.execute(
            """SELECT quantityRequested, reqStatus FROM MarketRequests
               WHERE requestID = %s FOR UPDATE""", (requestID,),
        )
        requestRow = cursor.fetchone()
        cursor.execute(
            """SELECT unit FROM HarvestLots WHERE lotID = %s FOR UPDATE""",
            (lotID,),
        )
        lot = cursor.fetchone()
        cursor.execute(
            """SELECT requestID, lotID, quantityAllocated, allocStatus
               FROM Allocations WHERE allocationID = %s FOR UPDATE""",
            (allocationID,),
        )
        allocation = cursor.fetchone()
        if requestRow is None or lot is None or allocation is None:
            connection.rollback()
            return False, "The allocation is no longer available."
        if int(allocation[0]) != requestID or int(allocation[1]) != lotID:
            connection.rollback()
            return False, "The allocation changed. Please refresh."
        cursor.execute(
            """SELECT 1 FROM HarvestLots hl, CropCycle cc, OwnedBy o
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = o.farmID
               AND hl.lotID = %s AND o.userID = %s""", (lotID, userID),
        )
        if cursor.fetchone() is None:
            connection.rollback()
            return False, "This allocation does not belong to your farm."
        if str(requestRow[1]) == "canceled":
            connection.rollback()
            return False, "This market request is canceled."
        remaining, delivered = getReservedForAllocation(cursor, allocationID)
        if remaining <= 0 or cancellationQuantity > remaining:
            connection.rollback()
            return False, f"Only {remaining:,.2f} {lot[0]} can be canceled."

        savedReason = (reason or "").strip() or "Reserved quantity released"
        cursor.execute(
            """INSERT INTO StockMovements
               (lotID, allocationID, movementType, quantity, unit,
                dateOccurred, reason, performedBy)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (lotID, allocationID, "canceled", cancellationQuantity, lot[0],
             datetime.now(), savedReason, userID),
        )
        remainingAfter = remaining - cancellationQuantity
        newAllocationStatus = (
            "delivered" if remainingAfter == 0 and delivered > 0 else
            "canceled" if remainingAfter == 0 else
            "partiallyDelivered" if delivered > 0 else "reserved"
        )
        cursor.execute(
            """UPDATE Allocations SET allocStatus = %s
               WHERE allocationID = %s""", (newAllocationStatus, allocationID),
        )
        requested, received, reserved, oldStatus = getLockedRequestProgress(
            cursor, requestID,
        )
        updateRequestStatus(cursor, requestID,
                            requestStatusFromProgress(requested, received, reserved, oldStatus),
                            userID, savedReason)
        connection.commit()
        logActivity(userID, "reservation_released",
                    f"Released {cancellationQuantity} {lot[0]} from allocation {allocationID}")
        return True, f"{cancellationQuantity:,.2f} {lot[0]} was released successfully."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Cancellation error:", repr(error))
        return False, "The reserved quantity could not be canceled."
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# -----------------------------------------------------------------------------
# User pages
# -----------------------------------------------------------------------------

REQUEST_STATUS_LABELS = {
    "pending": ("Waiting for farms", "wait"),
    "partiallyFulfilled": ("Partly filled", "part"),
    "fullyFulfilled": ("Filled", "done"),
    "canceled": ("Cancelled", "off"),
}


def requestCardHtml(request):
    from html import escape

    unit = str(request.unit)
    requested = float(request.quantityRequested)
    received = float(request.quantityReceived)
    reserved = float(request.quantityReserved)
    remaining = float(request.quantityRemaining)
    shortfall = float(request.quantityAllocated) - received - reserved
    label, tone = REQUEST_STATUS_LABELS.get(str(request.status), (str(request.status), "off"))

    receivedPercent = min(100.0, received / requested * 100) if requested else 0
    reservedPercent = min(100.0 - receivedPercent, reserved / requested * 100) if requested else 0
    if request.status == "canceled":
        neededLabel = "Not needed"
    elif request.status == "fullyFulfilled":
        neededLabel = "Complete"
    elif remaining <= 0.001:
        neededLabel = "Fully reserved"
    else:
        neededLabel = f"Still needed {formatQuantity(remaining, unit)}"

    icon = '<span class="material-symbols-rounded" aria-hidden="true">{}</span>'
    foot = [f'<span>{icon.format("storefront")}{escape(str(request.marketName))} · {escape(str(request.marketLocation))}</span>']
    if shortfall > 0.001:
        foot.append(f'<span class="hl-mr-short">{icon.format("error")}{escape(formatQuantity(shortfall, unit))} short on delivery</span>')

    return (
        f'<article class="hl-mr hl-mr-{tone}"><div class="hl-mr-top">'
        f'<span class="hl-mr-initial">{escape(str(request.cropName)[:1])}</span>'
        f'<div class="hl-mr-title"><strong>{escape(str(request.cropName))}</strong>'
        f'<small>Request #{int(request.requestID)} · {readableDate(request.requestDate) or "date not set"}</small></div>'
        f'<span class="hl-alloc-pill hl-alloc-pill-{tone}">{escape(label)}</span></div>'
        f'<div class="hl-produce-available"><b>{escape(formatQuantity(requested, unit))}</b> requested</div>'
        f'<div class="hl-produce-bar"><span class="hl-bar-available" style="width:{receivedPercent:.1f}%"></span>'
        f'<span class="hl-bar-reserved" style="width:{reservedPercent:.1f}%"></span></div>'
        f'<div class="hl-mr-legend"><span><i class="hl-dot-available"></i>Received {escape(formatQuantity(received, unit))}</span>'
        f'<span><i class="hl-dot-reserved"></i>Reserved {escape(formatQuantity(reserved, unit))}</span>'
        f'<span><i class="hl-dot-needed"></i>{escape(neededLabel)}</span></div>'
        f'<div class="hl-mr-foot">{"".join(foot)}</div></article>'
    )


@st.dialog("New request", width="medium")
def newMarketRequestDialog(current_user, markets, crops, units):
    marketOptions = {
        f"{market.marketName} · {market.location}": int(market.marketID)
        for market in markets.itertuples(index=False)
    }
    cropOptions = {
        str(crop.cropName): int(crop.cropID)
        for crop in crops.itertuples(index=False)
    }

    st.markdown(
        '<div class="hl-release-head"><strong>Ask farms for produce</strong>'
        '<span>Any farm with this crop in the same unit can fill your request.</span></div>',
        unsafe_allow_html=True
    )

    with st.form("new_request_form", border=False):
        marketLabel = st.selectbox("Deliver to", list(marketOptions.keys()))
        cropLabel = st.selectbox("Crop", list(cropOptions.keys()))

        amountColumn, unitColumn = st.columns([2, 1])
        quantity = amountColumn.number_input(
            "Amount",
            min_value=0.01,
            value=1.0
        )
        unit = unitColumn.selectbox(
            "Unit",
            units,
            index=units.index("kg") if "kg" in units else 0,
            help="Farms can only fill this from stock recorded in the same unit."
        )

        submitted = st.form_submit_button(
            "Send request",
            type="primary",
            icon=":material/send:",
            width="stretch"
        )

    if submitted:
        created, message = createProduceRequest(
            current_user=current_user,
            cropID=cropOptions[cropLabel],
            marketID=marketOptions[marketLabel],
            quantity=quantity,
            unit=unit
        )

        if created:
            st.session_state.requestMessage = message
            st.rerun()

        else:
            st.warning(message)


def requestSummaryHtml(request):
    from html import escape

    return (
        f'<div class="hl-release-head"><strong>{escape(str(request.cropName))} · Request #{int(request.requestID)}</strong>'
        f'<span>{escape(formatQuantity(request.quantityRequested, request.unit))} for '
        f'{escape(str(request.marketName))} · {escape(str(request.marketLocation))}</span></div>'
    )


@st.dialog("Edit request", width="medium")
def editMarketRequestDialog(request, userID):
    st.markdown(requestSummaryHtml(request), unsafe_allow_html=True)

    with st.form(f"edit_request_form_{int(request.requestID)}", border=False):
        newQuantity = st.number_input(
            f"New amount ({request.unit})",
            min_value=0.01,
            value=float(request.quantityRequested),
            help="The unit can't be changed. Cancel and make a new request to use another unit."
        )

        submitted = st.form_submit_button(
            "Save changes",
            type="primary",
            icon=":material/check:",
            width="stretch"
        )

    if submitted:
        changed, message = updateMarketRequest(
            requestID=int(request.requestID),
            userID=userID,
            newQuantity=newQuantity,
            action="Update quantity"
        )

        if changed:
            st.session_state.requestMessage = message
            st.rerun()

        else:
            st.warning(message)


@st.dialog("Cancel request", width="medium")
def cancelMarketRequestDialog(request, userID):
    st.markdown(requestSummaryHtml(request), unsafe_allow_html=True)
    st.markdown(
        '<div class="hl-alloc-note"><span class="material-symbols-rounded" aria-hidden="true">warning</span>'
        '<span>Farms will no longer see this request. This can\'t be undone.</span></div>',
        unsafe_allow_html=True
    )

    with st.form(f"cancel_request_form_{int(request.requestID)}", border=False):
        cancellationReason = st.text_input(
            "Reason for cancelling",
            placeholder="e.g. No longer needed this week"
        )

        submitted = st.form_submit_button(
            "Yes, cancel request",
            type="primary",
            icon=":material/close:",
            width="stretch"
        )

    if submitted:
        changed, message = updateMarketRequest(
            requestID=int(request.requestID),
            userID=userID,
            newQuantity=float(request.quantityRequested),
            action="Cancel request",
            cancellationReason=cancellationReason
        )

        if changed:
            st.session_state.requestMessage = message
            st.rerun()

        else:
            st.warning(message)


def showMarketRequests(current_user):
    st.header("My market requests")

    if current_user["role"] != "user":
        denyAccess(current_user, "Attempted to open buyer request management")
        return

    userID = int(current_user["userID"])

    if "requestMessage" in st.session_state:
        st.success(
            st.session_state.requestMessage
        )

        del st.session_state.requestMessage

    markets = selectData(
        """SELECT marketID, marketName, location
           FROM Markets
           WHERE isActive = 1
           ORDER BY marketName"""
    )

    crops = selectData(
        """SELECT cropID, cropName
           FROM Crops
           WHERE isActive = 1
           ORDER BY cropName"""
    )

    units = getActiveUnits()
    canCreate = not (markets.empty or crops.empty or not units)

    requests = selectData(
        """SELECT mr.requestID, m.marketName, m.location AS marketLocation, c.cropName, mr.quantityRequested, mr.unit, mr.requestDate, mr.reqStatus AS status
           FROM MarketRequests mr, Markets m, Crops c
           WHERE mr.marketID = m.marketID AND mr.cropID = c.cropID AND mr.userID = %s
           ORDER BY mr.requestDate DESC""",
        (userID,)
    )

    if requests.empty:
        requests = pd.DataFrame(columns=[
            "requestID", "marketName", "marketLocation", "cropName",
            "quantityRequested", "unit", "requestDate", "status"
        ])

    requests["quantityRequested"] = pd.to_numeric(
        requests["quantityRequested"]
    )

    requests["quantityAllocated"] = 0.0
    requests["quantityReceived"] = 0.0
    requests["quantityReserved"] = 0.0
    requests["quantityRemaining"] = 0.0

    for index, request in requests.iterrows():
        progress = calculateRequestProgress(int(request["requestID"]))

        if progress is None:
            continue

        requests.at[index, "quantityAllocated"] = progress["allocatedQuantity"]
        requests.at[index, "quantityReceived"] = progress["receivedQuantity"]
        requests.at[index, "quantityReserved"] = progress["outstandingReserved"]
        requests.at[index, "quantityRemaining"] = progress["remainingQuantity"]

    statusCounts = requests["status"].value_counts().to_dict()
    statusOptions = ["All"] + [
        status for status in REQUEST_STATUS_LABELS
        if statusCounts.get(status)
    ]

    def statusChipLabel(status):
        if status == "All":
            return f"All ({len(requests)})"
        return f"{REQUEST_STATUS_LABELS[status][0]} ({statusCounts[status]})"

    selectedStatus = "All"
    selectedMarket = "All"
    selectedCrop = "All"
    requestedAfter = None

    with st.container(
        horizontal=True,
        vertical_alignment="center",
        gap="small",
        key="mr_filters"
    ):
        if not requests.empty:
            selectedStatus = st.pills(
                "Status",
                statusOptions,
                format_func=statusChipLabel,
                default="All",
                key="mr_status",
                label_visibility="collapsed"
            ) or "All"

            with st.popover("More filters", icon=":material/tune:"):
                selectedMarket = st.selectbox(
                    "Market",
                    ["All"] + sorted(requests["marketName"].unique().tolist()),
                    key="mr_market"
                )
                selectedCrop = st.selectbox(
                    "Crop",
                    ["All"] + sorted(requests["cropName"].unique().tolist()),
                    key="mr_crop"
                )
                requestedAfter = st.date_input(
                    "Requested on or after",
                    value=None,
                    key="mr_date"
                )

        if st.button(
            "New request",
            key="mr_new",
            type="primary",
            icon=":material/add:",
            disabled=not canCreate
        ):
            newMarketRequestDialog(current_user, markets, crops, units)

    if not canCreate:
        st.info(
            "Markets and crops must exist, and the application must have "
            "at least one supported unit configured."
        )

    if requests.empty:
        st.markdown(
            '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
            'receipt_long</span><span>You haven\'t made any requests yet. Find produce in the Shop, '
            'or click New request.</span></div>',
            unsafe_allow_html=True
        )
        return

    filteredRequests = requests.copy()

    if selectedStatus != "All":
        filteredRequests = filteredRequests[filteredRequests["status"] == selectedStatus]

    if selectedMarket != "All":
        filteredRequests = filteredRequests[filteredRequests["marketName"] == selectedMarket]

    if selectedCrop != "All":
        filteredRequests = filteredRequests[filteredRequests["cropName"] == selectedCrop]

    if requestedAfter is not None:
        requestDates = pd.to_datetime(filteredRequests["requestDate"]).dt.date
        filteredRequests = filteredRequests[requestDates >= requestedAfter]

    filterNotes = [
        note for note in (
            selectedMarket if selectedMarket != "All" else None,
            selectedCrop if selectedCrop != "All" else None,
            f"from {readableDate(requestedAfter)}" if requestedAfter else None,
        ) if note
    ]
    if filterNotes:
        st.caption("Filtered by " + ", ".join(filterNotes))

    if filteredRequests.empty:
        st.markdown(
            '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
            'search_off</span><span>No requests matched these filters.</span></div>',
            unsafe_allow_html=True
        )

    else:
        cards = list(filteredRequests.itertuples(index=False))
        for cardStart in range(0, len(cards), 2):
            columns = st.columns(2)
            for column, request in zip(columns, cards[cardStart:cardStart + 2]):
                requestID = int(request.requestID)
                with column, st.container(key=f"mr_card_{requestID}"):
                    st.markdown(requestCardHtml(request), unsafe_allow_html=True)

                    if request.status == "pending":
                        with st.container(horizontal=True, gap="small", key=f"mr_actions_{requestID}"):
                            if st.button(
                                "Edit amount",
                                key=f"mr_edit_{requestID}",
                                icon=":material/edit:",
                                width="stretch"
                            ):
                                editMarketRequestDialog(request, userID)

                            if st.button(
                                "Cancel request",
                                key=f"mr_cancel_{requestID}",
                                icon=":material/close:",
                                width="stretch"
                            ):
                                cancelMarketRequestDialog(request, userID)


def updateMarketRequest(
    requestID,
    userID,
    newQuantity,
    action,
    cancellationReason=""
):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT reqStatus
               FROM MarketRequests
               WHERE requestID = %s AND userID = %s
               FOR UPDATE""",
            (requestID, userID)
        )

        request = cursor.fetchone()

        if request is None:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to update market request {requestID}"
            )
            return False, (
                "This request does not belong to your account."
            )

        currentStatus = str(request[0])

        if currentStatus != "pending":
            return False, (
                "Only pending requests can be changed."
            )

        cursor.execute(
            """SELECT allocationID
               FROM Allocations
               WHERE requestID = %s""",
            (requestID,)
        )

        existingAllocation = cursor.fetchone()

        if existingAllocation is not None:
            return False, (
                "This request cannot be changed because "
                "an allocation has already been created."
            )

        if action == "Update quantity":
            if newQuantity <= 0:
                return False, (
                    "The requested quantity must be greater than zero."
                )

            cursor.execute(
                """UPDATE MarketRequests
                   SET quantityRequested = %s
                   WHERE requestID = %s AND userID = %s""",
                (
                    newQuantity,
                    requestID,
                    userID
                )
            )

            message = "Request quantity updated successfully."

        else:
            cancellationReason = cancellationReason.strip()

            if not cancellationReason:
                return False, (
                    "Enter a reason for canceling the request."
                )

            updateRequestStatus(
                cursor=cursor,
                requestID=requestID,
                newStatus="canceled",
                changedBy=userID,
                reason=cancellationReason
            )

            message = "Request canceled successfully."

        connection.commit()
        return True, message

    except Exception as error:
        if connection:
            connection.rollback()

        print("Request update error:", error)

        return False, (
            "The request could not be updated."
        )

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


def getAllocationReservedForDisplay(allocationID):
    movements = selectData(
        """SELECT movementType, quantity FROM StockMovements
           WHERE allocationID = %s""", (allocationID,), show_error=False
    )
    balance = Decimal("0")
    for _, row in movements.iterrows():
        amount = Decimal(str(row["quantity"]))
        if row["movementType"] == "reserved":
            balance += amount
        elif row["movementType"] in ("canceled", "delivered"):
            balance -= amount
    return max(Decimal("0"), balance)


def deliveryCardHtml(allocation):
    from html import escape

    unit = str(allocation.unit)
    expected = float(allocation.expectedNow)
    released = float(allocation.quantityAllocated) - expected
    statusText = "Partly delivered" if str(allocation.status) == "partiallyDelivered" else "On the way"

    def fact(iconName, label, value, detail):
        return (
            f'<div class="hl-dl-fact"><span class="material-symbols-rounded" aria-hidden="true">{iconName}</span>'
            f'<div><small>{label}</small><b>{escape(value)}</b><em>{escape(detail)}</em></div></div>'
        )

    facts = (
        fact("agriculture", "From", str(allocation.farmName), str(allocation.farmLocation))
        + fact("storefront", "To", str(allocation.marketName), str(allocation.marketLocation))
        + fact("event", "Reserved", readableDate(allocation.allocationDate) or "Date not set",
               f"Allocation #{int(allocation.allocationID)}")
    )
    releasedNote = ""
    if released > 0.001:
        releasedNote = (
            '<div class="hl-dl-note"><span class="material-symbols-rounded" aria-hidden="true">undo</span>'
            f'The farm released {escape(formatQuantity(released, unit))} of the '
            f'{escape(formatQuantity(float(allocation.quantityAllocated), unit))} reserved</div>'
        )

    return (
        f'<article class="hl-mr"><div class="hl-mr-top">'
        f'<span class="hl-mr-initial">{escape(str(allocation.cropName)[:1])}</span>'
        f'<div class="hl-mr-title"><strong>{escape(str(allocation.cropName))}</strong>'
        f'<small>Request #{int(allocation.requestID)} · Lot #{int(allocation.lotID)}</small></div>'
        f'<span class="hl-alloc-pill hl-alloc-pill-part">{statusText}</span></div>'
        f'<div class="hl-produce-available hl-dl-amount"><b>{escape(formatQuantity(expected, unit))}</b> expected</div>'
        f'<div class="hl-dl-facts">{facts}</div>{releasedNote}</article>'
    )


@st.dialog("Confirm delivery", width="medium")
def confirmDeliveryDialog(allocation, userID):
    from html import escape

    allocationID = int(allocation.allocationID)
    unit = str(allocation.unit)
    expected = float(allocation.expectedNow)

    st.markdown(
        f'<div class="hl-release-head"><strong>{escape(str(allocation.cropName))} · Request #{int(allocation.requestID)}</strong>'
        f'<span>{escape(formatQuantity(expected, unit))} expected from {escape(str(allocation.farmName))} '
        f'for {escape(str(allocation.marketName))}. Enter what actually arrived.</span></div>',
        unsafe_allow_html=True
    )

    allocatedOn = pd.to_datetime(allocation.allocationDate, errors="coerce")
    earliestDate = None if pd.isna(allocatedOn) else allocatedOn.date()
    if earliestDate is not None and earliestDate > date.today():
        earliestDate = None

    amountColumn, dateColumn = st.columns(2)
    receivedQuantity = amountColumn.number_input(
        f"Amount received ({unit})",
        min_value=0.0,
        max_value=expected,
        value=expected,
        key=f"dl_received_{allocationID}",
        help="Enter 0 if nothing arrived."
    )
    deliveryDate = dateColumn.date_input(
        "Delivery date",
        value=date.today(),
        min_value=earliestDate,
        max_value=date.today(),
        key=f"dl_date_{allocationID}"
    )

    shortfall = expected - float(receivedQuantity)
    if shortfall > 0.001:
        st.markdown(
            '<div class="hl-alloc-note"><span class="material-symbols-rounded" aria-hidden="true">warning</span>'
            f'<span>{escape(formatQuantity(shortfall, unit))} short. The missing amount goes back to the '
            "farm's stock, and a receipt can't be changed once confirmed.</span></div>",
            unsafe_allow_html=True
        )

    if st.button(
        "Confirm delivery",
        type="primary",
        icon=":material/check:",
        width="stretch",
        key=f"dl_confirm_{allocationID}"
    ):
        confirmed, message = confirmDelivery(
            allocationID,
            receivedQuantity,
            deliveryDate,
            userID
        )

        if confirmed:
            st.session_state.deliveryMessage = message
            st.rerun()

        else:
            st.warning(message)


def showDeliveries(current_user):
    st.header("My deliveries")

    if current_user["role"] != "user":
        denyAccess(current_user, "Attempted to open buyer delivery confirmation")
        return

    userID = int(current_user["userID"])

    if "deliveryMessage" in st.session_state:
        st.success(st.session_state.deliveryMessage)
        del st.session_state.deliveryMessage

    allocations = selectData(
        """SELECT a.allocationID, a.requestID, a.lotID, c.cropName,
                  a.quantityAllocated, mr.unit, a.allocationDate,
                  a.allocStatus AS status, f.farmName, f.location AS farmLocation,
                  m.marketName, m.location AS marketLocation
           FROM Allocations a, MarketRequests mr, Crops c, HarvestLots hl,
                CropCycle cc, Farms f, Markets m
           WHERE a.requestID = mr.requestID AND mr.cropID = c.cropID
           AND a.lotID = hl.lotID AND hl.cycleID = cc.cycleID
           AND cc.farmID = f.farmID AND mr.marketID = m.marketID
           AND mr.userID = %s
           AND a.allocStatus IN ('reserved', 'partiallyDelivered')
           AND NOT EXISTS (
               SELECT 1 FROM DeliveryReceipts dr
               WHERE dr.allocationID = a.allocationID
           )
           ORDER BY a.allocationDate""",
        (userID,)
    )

    if not allocations.empty:
        # Show the buyer only the amount still reserved after cancellations.
        allocations["expectedNow"] = allocations["allocationID"].apply(
            lambda aid: float(getAllocationReservedForDisplay(int(aid)))
        )
        allocations = allocations[allocations["expectedNow"] > 0].copy()

    st.subheader(f"On the way ({len(allocations)})")

    if allocations.empty:
        st.markdown(
            '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
            'local_shipping</span><span>Nothing on the way right now. Deliveries show up here once a '
            'farm reserves stock for one of your requests.</span></div>',
            unsafe_allow_html=True
        )

    else:
        cards = list(allocations.itertuples(index=False))
        for cardStart in range(0, len(cards), 2):
            columns = st.columns(2) if len(cards) > 1 else [st.container()]
            for column, allocation in zip(columns, cards[cardStart:cardStart + 2]):
                allocationID = int(allocation.allocationID)
                with column, st.container(key=f"dl_card_{allocationID}"):
                    st.markdown(deliveryCardHtml(allocation), unsafe_allow_html=True)

                    if st.button(
                        "Confirm delivery",
                        key=f"dl_open_{allocationID}",
                        type="primary",
                        icon=":material/inventory:",
                        width="stretch"
                    ):
                        confirmDeliveryDialog(allocation, userID)

    receipts = selectData(
        """SELECT dr.receiptID, dr.requestID, dr.allocationID, c.cropName,
                  m.marketName, dr.expectedQuantity, dr.receivedQuantity,
                  dr.discrepancyQuantity, dr.unit, dr.deliveryDate
           FROM DeliveryReceipts dr, MarketRequests mr, Crops c, Markets m
           WHERE dr.requestID = mr.requestID AND mr.cropID = c.cropID
           AND mr.marketID = m.marketID
           AND mr.userID = %s
           ORDER BY dr.deliveryDate DESC""",
        (userID,)
    )

    with st.container(horizontal=True, vertical_alignment="center", key="dl_receipts_head"):
        st.subheader(f"Delivery receipts ({len(receipts)})")
        view = st.segmented_control(
            "Show as",
            ["List", "Table"],
            default="List",
            key="dl_view",
            on_change=keepDeliveryView,
            label_visibility="collapsed"
        )

    if receipts.empty:
        st.markdown(
            '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
            'receipt_long</span><span>You haven\'t confirmed any deliveries yet.</span></div>',
            unsafe_allow_html=True
        )

    elif view == "Table":
        deliveredOn = pd.to_datetime(receipts["deliveryDate"], errors="coerce")
        display = pd.DataFrame({
            "Receipt #": receipts["receiptID"],
            "Request #": receipts["requestID"],
            "Crop": receipts["cropName"],
            "Market": receipts["marketName"],
            "Expected": receipts["expectedQuantity"],
            "Received": receipts["receivedQuantity"],
            "Short by": receipts["discrepancyQuantity"],
            "Unit": receipts["unit"],
            "Delivered on": deliveredOn.dt.strftime("%d %b %Y"),
        })
        showTable(display, use_container_width=True, hide_index=True)

    else:
        rows = "".join(receiptRowHtml(receipt) for receipt in receipts.itertuples(index=False))
        st.markdown(f'<div class="hl-alloc-list">{rows}</div>', unsafe_allow_html=True)


def keepDeliveryView():
    if st.session_state.get("dl_view") is None:
        st.session_state["dl_view"] = st.session_state.get("dl_view_last", "List")
    st.session_state["dl_view_last"] = st.session_state["dl_view"]


def receiptRowHtml(receipt):
    from html import escape

    unit = str(receipt.unit)
    delivered = pd.to_datetime(receipt.deliveryDate, errors="coerce")
    dayText, monthText = ("—", "") if pd.isna(delivered) else (str(delivered.day), delivered.strftime("%b").upper())
    shortBy = float(receipt.discrepancyQuantity)
    if shortBy > 0.001:
        pill = f'<span class="hl-alloc-pill">Short by {escape(formatQuantity(shortBy, unit))}</span>'
    else:
        pill = '<span class="hl-alloc-pill hl-alloc-pill-done">Complete</span>'
    return (
        f'<div class="hl-alloc-row"><div class="hl-alloc-date"><b>{dayText}</b><span>{monthText}</span></div>'
        f'<div class="hl-alloc-main"><strong>{escape(str(receipt.cropName))} · '
        f'{escape(formatQuantity(float(receipt.receivedQuantity), unit))} of '
        f'{escape(formatQuantity(float(receipt.expectedQuantity), unit))} received</strong>'
        f'<small>{escape(str(receipt.marketName))} · Delivered {readableDate(receipt.deliveryDate) or "date not set"}</small>'
        f'<small>Receipt #{int(receipt.receiptID)} · Request #{int(receipt.requestID)} · '
        f'Allocation #{int(receipt.allocationID)}</small></div>'
        f'<div class="hl-alloc-side">{pill}</div></div>'
    )


def confirmDelivery(allocationID, receivedQuantity, deliveryDate, userID):
    connection = None
    cursor = None
    try:
        receivedQuantity = Decimal(str(receivedQuantity))
        if not receivedQuantity.is_finite() or receivedQuantity < 0:
            return False, "Received quantity cannot be negative or invalid."
        if hasattr(deliveryDate, "date"):
            deliveryDate = deliveryDate.date()
        if deliveryDate > date.today():
            return False, "The delivery date cannot be in the future."
        connection = getConnection()
        cursor = connection.cursor()
        # Read the latest committed movements after waiting for a lock.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cursor.execute(
            """SELECT requestID, lotID FROM Allocations
               WHERE allocationID = %s""", (allocationID,),
        )
        ids = cursor.fetchone()
        if ids is None:
            connection.rollback()
            return False, "The allocation could not be found."
        requestID, lotID = int(ids[0]), int(ids[1])

        # Lock the same records so a second delivery cannot sneak in.
        cursor.execute(
            """SELECT userID, quantityRequested, unit, reqStatus
               FROM MarketRequests WHERE requestID = %s FOR UPDATE""",
            (requestID,),
        )
        request = cursor.fetchone()
        cursor.execute(
            """SELECT unit FROM HarvestLots WHERE lotID = %s FOR UPDATE""",
            (lotID,),
        )
        lot = cursor.fetchone()
        cursor.execute(
            """SELECT requestID, lotID, quantityAllocated, allocationDate, allocStatus
               FROM Allocations WHERE allocationID = %s FOR UPDATE""",
            (allocationID,),
        )
        allocation = cursor.fetchone()
        if request is None or lot is None or allocation is None:
            connection.rollback()
            return False, "This delivery is no longer available."
        if int(request[0]) != int(userID):
            connection.rollback()
            return False, "You cannot confirm another user's delivery."
        if int(allocation[0]) != requestID or int(allocation[1]) != lotID:
            connection.rollback()
            return False, "The allocation changed. Please refresh."
        if str(request[3]) == "canceled" or str(allocation[4]) not in (
            "reserved", "partiallyDelivered"
        ):
            connection.rollback()
            return False, "This allocation is not awaiting confirmation."
        if str(request[2]) != str(lot[0]):
            connection.rollback()
            return False, "The request and harvest units do not match."
        if deliveryDate < pd.to_datetime(allocation[3]).date():
            connection.rollback()
            return False, "Delivery cannot be dated before the allocation."
        cursor.execute(
            """SELECT receiptID FROM DeliveryReceipts
               WHERE allocationID = %s""", (allocationID,),
        )
        if cursor.fetchone() is not None:
            connection.rollback()
            return False, "A delivery receipt already exists for this allocation."

        # Expected means what is still reserved after earlier cancellations.
        expectedQuantity, alreadyDelivered = getReservedForAllocation(cursor, allocationID)
        if expectedQuantity <= 0:
            connection.rollback()
            return False, "There is no reserved quantity left to deliver."
        if receivedQuantity > expectedQuantity:
            connection.rollback()
            return False, f"Received quantity cannot exceed {expectedQuantity:,.2f} {lot[0]}."
        requested, receivedSoFar, otherReserved, oldStatus = getLockedRequestProgress(
            cursor, requestID,
        )
        if receivedSoFar + receivedQuantity > requested:
            connection.rollback()
            return False, "The received quantity exceeds the market request."
        discrepancy = expectedQuantity - receivedQuantity
        movementDate = datetime.combine(deliveryDate, datetime.min.time())
        cursor.execute(
            """INSERT INTO DeliveryReceipts
               (requestID, allocationID, expectedQuantity, receivedQuantity,
                discrepancyQuantity, unit, deliveryDate, confirmedBy)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (requestID, allocationID, expectedQuantity, receivedQuantity,
             discrepancy, lot[0], movementDate, userID),
        )
        if receivedQuantity > 0:
            cursor.execute(
                """INSERT INTO StockMovements
                   (lotID, allocationID, movementType, quantity, unit,
                    dateOccurred, reason, performedBy)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (lotID, allocationID, "delivered", receivedQuantity, lot[0],
                 movementDate, "Delivery confirmed by buyer", userID),
            )
        if discrepancy > 0:
            cursor.execute(
                """INSERT INTO StockMovements
                   (lotID, allocationID, movementType, quantity, unit,
                    dateOccurred, reason, performedBy)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (lotID, allocationID, "canceled", discrepancy, lot[0],
                 movementDate, "Undelivered stock released", userID),
            )
        cursor.execute(
            """UPDATE Allocations SET allocStatus = 'delivered'
               WHERE allocationID = %s""", (allocationID,),
        )
        requested, received, reserved, oldStatus = getLockedRequestProgress(cursor, requestID)
        updateRequestStatus(cursor, requestID,
                            requestStatusFromProgress(requested, received, reserved, oldStatus),
                            userID, "Delivery receipt confirmed")
        connection.commit()
        logActivity(userID, "delivery_confirmed",
                    f"Confirmed allocation {allocationID}; discrepancy {discrepancy} {lot[0]}")
        return True, f"Delivery confirmed. Discrepancy: {discrepancy:,.2f} {lot[0]}."
    except Exception as error:
        if connection:
            connection.rollback()
        print("Delivery confirmation error:", repr(error))
        return False, "The delivery could not be confirmed. Please try again."
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# -----------------------------------------------------------------------------
# Administrator pages
# -----------------------------------------------------------------------------

ROLE_LABELS = {
    "farmer": ("Farmer", "Farmers", "hl-alloc-pill-part"),
    "user": ("Market buyer", "Market buyers", "hl-role-buyer"),
    "admin": ("Administrator", "Administrators", "hl-alloc-pill-done"),
}


def roleLabel(role):
    return ROLE_LABELS.get(str(role), (str(role).title(),))[0]


def userRowHtml(user, isSelf):
    from html import escape

    name = f"{user.fname} {user.lname}"
    label, _, pillClass = ROLE_LABELS.get(str(user.role), (str(user.role), "", "hl-alloc-pill-off"))
    youTag = '<span class="hl-you">You</span>' if isSelf else ""
    return (
        f'<div class="hl-urow"><span class="hl-mr-initial">{escape(str(user.fname)[:1].upper())}</span>'
        f'<div class="hl-alloc-main"><strong>{escape(name)}{youTag}</strong>'
        f'<small>{escape(str(user.email))}</small></div>'
        f'<span class="hl-alloc-pill {pillClass}">{escape(label)}</span></div>'
    )


@st.dialog("Change role", width="medium")
def changeUserRoleDialog(user, current_user):
    from html import escape

    userID = int(user.userID)
    st.markdown(
        f'<div class="hl-release-head"><strong>{escape(f"{user.fname} {user.lname}")}</strong>'
        f'<span>{escape(str(user.email))} · currently {escape(roleLabel(user.role))}</span></div>',
        unsafe_allow_html=True
    )

    roleOptions = sorted(roles)
    with st.form(f"admin_change_role_form_{userID}", border=False):
        newRole = st.selectbox(
            "New role",
            roleOptions,
            index=roleOptions.index(str(user.role)) if str(user.role) in roleOptions else 0,
            format_func=roleLabel,
            help="Farmers manage farms and stock, market buyers request produce, and administrators manage the system."
        )
        submitted = st.form_submit_button(
            "Save role",
            type="primary",
            icon=":material/check:",
            width="stretch"
        )

    if submitted:
        if userID == int(current_user["userID"]):
            st.warning("You cannot change your own administrator role here.")
        elif changeData(
            "UPDATE Users SET role = %s WHERE userID = %s",
            (newRole, userID)
        ):
            logActivity(
                current_user["userID"],
                "admin_role_changed",
                f"Changed user {userID} to {newRole}"
            )
            st.session_state.adminUserMessage = "User role updated successfully."
            st.rerun()


@st.dialog("Add user", width="medium")
def addUserDialog(current_user):
    st.markdown(
        '<div class="hl-release-head"><strong>Create an account</strong>'
        '<span>A new farmer also gets their first farm, which you can rename later.</span></div>',
        unsafe_allow_html=True
    )

    nameColumns = st.columns(2)
    firstName = nameColumns[0].text_input("First name", key="admin_new_fname")
    lastName = nameColumns[1].text_input("Last name", key="admin_new_lname")
    email = st.text_input("Email", key="admin_new_email")
    roleColumns = st.columns(2)
    role = roleColumns[0].selectbox("Role", sorted(roles), format_func=roleLabel, key="admin_new_role")
    password = roleColumns[1].text_input("Demonstration password", type="password", key="admin_new_password")
    farm_name = ""
    farm_location = ""
    if role == "farmer":
        farmColumns = st.columns(2)
        farm_name = farmColumns[0].text_input("Farm name", key="admin_new_farm")
        farm_location = farmColumns[1].text_input("Farm location or emirate", key="admin_new_farm_location")

    if st.button("Add user", type="primary", icon=":material/check:", width="stretch", key="admin_new_submit"):
        if not firstName.strip() or not lastName.strip() or not email.strip() or not password:
            st.warning("Complete all user fields.")
        elif not isValidEmail(email.strip().lower()):
            st.warning("Enter a valid email address.")
        elif role == "farmer" and (
            not farm_name.strip() or not farm_location.strip()
        ):
            st.warning("Enter the farm name and location for the farmer.")
        else:
            connection = None
            cursor = None
            try:
                connection = getConnection()
                cursor = connection.cursor()
                cursor.execute(
                    """
                    INSERT INTO Users (fname, lname, role, email, passwordHash)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        firstName.strip(),
                        lastName.strip(),
                        role,
                        email.strip().lower(),
                        hashPassword(password),
                    ),
                )
                user_id = cursor.lastrowid

                if role == "farmer":
                    cursor.execute(
                        """
                        INSERT INTO Farms (farmName, location)
                        VALUES (%s, %s)
                        """,
                        (
                            farm_name.strip(),
                            farm_location.strip(),
                        ),
                    )
                    farmID = cursor.lastrowid
                    cursor.execute(
                        "INSERT INTO OwnedBy (userID, farmID) VALUES (%s, %s)",
                        (user_id, farmID)
                    )

                connection.commit()
                logActivity(
                    current_user["userID"],
                    "admin_user_created",
                    f"Created user {user_id}"
                )
                st.session_state.adminUserMessage = "User added successfully."
                st.rerun()
            except Exception as error:
                if connection:
                    connection.rollback()
                st.error(friendlyDatabaseError(error))
            finally:
                if cursor:
                    cursor.close()
                if connection:
                    connection.close()


def keepUserView():
    if st.session_state.get("adm_user_view") is None:
        st.session_state["adm_user_view"] = st.session_state.get("adm_user_view_last", "List")
    st.session_state["adm_user_view_last"] = st.session_state["adm_user_view"]


def showAdminUserManagement(current_user):
    st.header("Users and security")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator user management")
        return

    usersTab, auditTab = st.tabs(["User accounts", "Security activity"])

    with usersTab:
        if "adminUserMessage" in st.session_state:
            st.success(st.session_state.pop("adminUserMessage"))

        users = selectData(
            """SELECT userID, fname, lname, role, email
               FROM Users
               ORDER BY fname, lname"""
        )

        roleCounts = users["role"].value_counts().to_dict() if not users.empty else {}
        roleOptions = ["All"] + [role for role in ROLE_LABELS if roleCounts.get(role)]

        def roleChipLabel(role):
            if role == "All":
                return f"All ({len(users)})"
            return f"{ROLE_LABELS[role][1]} ({roleCounts[role]})"

        selectedRole = "All"
        search = ""
        view = "List"

        with st.container(
            horizontal=True,
            vertical_alignment="center",
            gap="small",
            key="adm_user_filters"
        ):
            if not users.empty:
                selectedRole = st.pills(
                    "Role",
                    roleOptions,
                    format_func=roleChipLabel,
                    default="All",
                    key="adm_user_role",
                    label_visibility="collapsed"
                ) or "All"
                search = st.text_input(
                    "Search",
                    placeholder="Search name or email",
                    key="adm_user_search",
                    label_visibility="collapsed",
                    icon=":material/search:"
                )
                view = st.segmented_control(
                    "Show as",
                    ["List", "Table"],
                    default="List",
                    key="adm_user_view",
                    on_change=keepUserView,
                    label_visibility="collapsed"
                )

            if st.button("Add user", key="adm_user_add", type="primary", icon=":material/person_add:"):
                addUserDialog(current_user)

        shown = users
        if selectedRole != "All":
            shown = shown[shown["role"] == selectedRole]
        if search.strip():
            needle = search.strip().lower()
            shown = shown[
                (shown["fname"] + " " + shown["lname"]).str.lower().str.contains(needle, regex=False)
                | shown["email"].str.lower().str.contains(needle, regex=False)
            ]

        if users.empty:
            st.info("There are no user accounts.")
        elif shown.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'person_search</span><span>No accounts match these filters.</span></div>',
                unsafe_allow_html=True
            )
        elif view == "Table":
            display = pd.DataFrame({
                "Name": shown["fname"] + " " + shown["lname"],
                "Email": shown["email"],
                "Role": shown["role"].map(roleLabel),
            })
            showTable(display, use_container_width=True, hide_index=True)
        else:
            selfID = int(current_user["userID"])
            with st.container(key="adm_user_list", gap=None):
                for user in shown.itertuples(index=False):
                    userID = int(user.userID)
                    isSelf = userID == selfID
                    with st.container(
                        horizontal=True,
                        vertical_alignment="center",
                        key=f"adm_urow_{userID}"
                    ):
                        st.markdown(userRowHtml(user, isSelf), unsafe_allow_html=True)
                        if st.button(
                            "Change role",
                            key=f"adm_role_{userID}",
                            icon=":material/manage_accounts:",
                            disabled=isSelf,
                            help="You can't change your own role." if isSelf else None
                        ):
                            changeUserRoleDialog(user, current_user)

    with auditTab:
        if "permissionTestMessage" in st.session_state:
            st.success(st.session_state.permissionTestMessage)
            del st.session_state.permissionTestMessage

        activity = selectData(
            """SELECT al.activityID,
                      CONCAT(u.fname, ' ', u.lname) AS performedBy,
                      u.role, al.actionType, al.actionDate, al.details
               FROM UserActivityLog al, Users u
               WHERE al.userID = u.userID
               ORDER BY al.actionDate DESC"""
        )

        if activity.empty:
            activity = pd.DataFrame(columns=[
                "activityID", "performedBy", "role", "actionType", "actionDate", "details"
            ])

        activity["group"] = activity["actionType"].map(activityGroup)
        groupCounts = activity["group"].value_counts().to_dict()
        groupOptions = ["All"] + [group for group in ACTIVITY_GROUPS if groupCounts.get(group)]

        def groupChipLabel(group):
            if group == "All":
                return f"All ({len(activity)})"
            return f"{ACTIVITY_GROUPS[group]} ({groupCounts[group]})"

        selectedGroup = "All"
        selectedAction = "All"
        selectedPerson = "All"
        view = "List"

        with st.container(
            horizontal=True,
            vertical_alignment="center",
            gap="small",
            key="adm_act_filters"
        ):
            if not activity.empty:
                selectedGroup = st.pills(
                    "Activity",
                    groupOptions,
                    format_func=groupChipLabel,
                    default="All",
                    key="adm_act_group",
                    label_visibility="collapsed"
                ) or "All"

                with st.popover("More filters", icon=":material/tune:"):
                    selectedAction = st.selectbox(
                        "Activity type",
                        ["All"] + sorted(activity["actionType"].unique().tolist(), key=activityLabel),
                        format_func=lambda action: action if action == "All" else activityLabel(action),
                        key="adm_act_type"
                    )
                    selectedPerson = st.selectbox(
                        "Person",
                        ["All"] + sorted(activity["performedBy"].unique().tolist()),
                        key="adm_act_person"
                    )

                view = st.segmented_control(
                    "Show as",
                    ["List", "Table"],
                    default="List",
                    key="adm_act_view",
                    on_change=keepActivityView,
                    label_visibility="collapsed"
                )

            if st.button("Test farm access", key="adm_act_test", icon=":material/shield:"):
                farmAccessTestDialog(current_user)

        shown = activity
        if selectedGroup != "All":
            shown = shown[shown["group"] == selectedGroup]
        if selectedAction != "All":
            shown = shown[shown["actionType"] == selectedAction]
        if selectedPerson != "All":
            shown = shown[shown["performedBy"] == selectedPerson]

        if activity.empty:
            st.info("No security activity has been recorded yet.")
        elif shown.empty:
            st.markdown(
                '<div class="hl-gallery-empty"><span class="material-symbols-rounded" aria-hidden="true">'
                'manage_search</span><span>No activity matches these filters.</span></div>',
                unsafe_allow_html=True
            )
        elif view == "Table":
            when = pd.to_datetime(shown["actionDate"], errors="coerce")
            display = pd.DataFrame({
                "When": when.dt.strftime("%d %b %Y, %H:%M"),
                "Person": shown["performedBy"],
                "Role": shown["role"].map(roleLabel),
                "Activity": shown["actionType"].map(activityLabel),
                "Details": shown["details"].fillna(""),
            })
            showTable(display, use_container_width=True, hide_index=True)
        else:
            limit = st.session_state.get("adm_act_limit", 40)
            rows = "".join(
                activityRowHtml(entry) for entry in shown.head(limit).itertuples(index=False)
            )
            st.markdown(f'<div class="hl-alloc-list hl-act-list">{rows}</div>', unsafe_allow_html=True)

            if len(shown) > limit:
                st.caption(f"Showing the latest {limit} of {len(shown)}.")
                if st.button("Show more", key="adm_act_more", icon=":material/expand_more:"):
                    st.session_state["adm_act_limit"] = limit + 40
                    st.rerun()


ACTIVITY_GROUPS = {
    "denied": "Denied",
    "changes": "Changes",
    "signin": "Sign-ins",
}

ACTIVITY_LABELS = {
    "login": "Signed in",
    "logout": "Signed out",
    "access_denied": "Access denied",
    "assistant_access_denied": "FittonAI refused a question",
    "assistant_query_blocked": "FittonAI blocked a query",
    "admin_role_changed": "Changed a user's role",
    "admin_user_created": "Added a user",
    "admin_crop_created": "Added a crop",
    "admin_crop_updated": "Updated a crop",
    "admin_market_created": "Added a market",
    "admin_market_updated": "Updated a market",
    "unit_added": "Added a unit",
    "unit_status_changed": "Changed a unit's status",
    "farm_created": "Added a farm",
    "harvest_recorded": "Recorded a harvest",
    "allocation_created": "Reserved stock",
    "reservation_released": "Released a reservation",
    "delivery_confirmed": "Confirmed a delivery",
}


def activityLabel(actionType):
    actionType = str(actionType)
    return ACTIVITY_LABELS.get(actionType, actionType.replace("_", " ").capitalize())


def activityGroup(actionType):
    actionType = str(actionType)
    if actionType in {"login", "logout"}:
        return "signin"
    if "denied" in actionType or "blocked" in actionType:
        return "denied"
    return "changes"


def activityRowHtml(entry):
    from html import escape

    when = pd.to_datetime(entry.actionDate, errors="coerce")
    dayText, monthText, timeText = ("—", "", "") if pd.isna(when) else (
        str(when.day), when.strftime("%b").upper(), when.strftime("%H:%M")
    )
    group = activityGroup(entry.actionType)
    pillClass, pillText = {
        "denied": ("hl-alloc-pill-wait", "Denied"),
        "changes": ("hl-alloc-pill-part", "Change"),
        "signin": ("hl-alloc-pill-off", "Sign-in"),
    }[group]
    details = "" if pd.isna(entry.details) or group == "signin" else str(entry.details)
    detailsHtml = f'<small>{escape(details)}</small>' if details else ""
    return (
        f'<div class="hl-alloc-row"><div class="hl-alloc-date"><b>{dayText}</b><span>{monthText}</span></div>'
        f'<div class="hl-alloc-main"><strong>{escape(activityLabel(entry.actionType))}</strong>'
        f'<small>{escape(str(entry.performedBy))} · {escape(roleLabel(entry.role))} · {timeText}</small>'
        f'{detailsHtml}</div>'
        f'<div class="hl-alloc-side"><span class="hl-alloc-pill {pillClass}">{pillText}</span></div></div>'
    )


def keepActivityView():
    if st.session_state.get("adm_act_view") is None:
        st.session_state["adm_act_view"] = st.session_state.get("adm_act_view_last", "List")
    st.session_state["adm_act_view_last"] = st.session_state["adm_act_view"]


@st.dialog("Test farm access", width="medium")
def farmAccessTestDialog(current_user):
    st.markdown(
        '<div class="hl-release-head"><strong>Check a farmer can only reach their own farms</strong>'
        '<span>Pick a farm the farmer doesn\'t own. The app should refuse access and record it in the activity log.</span></div>',
        unsafe_allow_html=True
    )

    farmers = selectData(
        """SELECT userID, fname, lname
           FROM Users
           WHERE role = 'farmer'
           ORDER BY fname, lname"""
    )
    farms = selectData(
        """SELECT farmID, farmName, location
           FROM Farms
           ORDER BY farmName"""
    )

    if farmers.empty or farms.empty:
        st.info("Add at least one farmer and farm first.")
        return

    farmerOptions = {
        f"{row.fname} {row.lname}": int(row.userID) for row in farmers.itertuples(index=False)
    }
    farmOptions = {
        f"{row.farmName} · {row.location}": int(row.farmID) for row in farms.itertuples(index=False)
    }

    columns = st.columns(2)
    selectedFarmer = columns[0].selectbox(
        "Farmer account",
        list(farmerOptions.keys()),
        key="permission_test_farmer"
    )
    selectedFarm = columns[1].selectbox(
        "Farm to test",
        list(farmOptions.keys()),
        key="permission_test_farm"
    )

    if st.button("Run test", type="primary", icon=":material/check:", width="stretch", key="permission_test_run"):
        testedUserID = farmerOptions[selectedFarmer]
        testedFarmID = farmOptions[selectedFarm]
        ownership = selectData(
            """SELECT userID
               FROM OwnedBy
               WHERE userID = %s AND farmID = %s""",
            (testedUserID, testedFarmID),
            show_error=False
        )

        if ownership.empty:
            logActivity(
                testedUserID,
                "access_denied",
                f"Permission test denied access to farm {testedFarmID}; tested by admin {current_user['userID']}"
            )
            st.session_state.permissionTestMessage = (
                "Access was correctly denied and recorded in the audit log."
            )
            st.rerun()
        else:
            st.warning(
                "This farmer owns the selected farm, so access is allowed. "
                "Choose an unassigned farm to demonstrate a denial."
            )


UNIT_NAMES = {
    "g": "Grams",
    "kg": "Kilograms",
    "ton": "Tonnes",
    "piece": "Pieces",
    "box": "Boxes",
}


def isActiveFlag(value):
    return bool(int(value)) if str(value).strip().isdigit() else bool(value)


def referenceRowHtml(title, subtitle, isActive):
    from html import escape

    pill = (
        '<span class="hl-alloc-pill hl-alloc-pill-part">Active</span>'
        if isActive else
        '<span class="hl-alloc-pill hl-alloc-pill-off">Inactive</span>'
    )
    return (
        f'<div class="hl-urow"><span class="hl-mr-initial">{escape(str(title)[:1].upper())}</span>'
        f'<div class="hl-alloc-main"><strong>{escape(str(title))}</strong>'
        f'<small>{escape(str(subtitle))}</small></div>{pill}</div>'
    )


def referenceHead(kind, title, count, note, buttonLabel):
    from html import escape

    with st.container(horizontal=True, vertical_alignment="center", key=f"adm_refhead_{kind}"):
        st.markdown(
            f'<div class="hl-ref-head"><strong>{escape(title)} ({count})</strong>'
            f'<span>{escape(note)}</span></div>',
            unsafe_allow_html=True
        )
        return st.button(
            buttonLabel,
            type="primary",
            icon=":material/add:",
            key=f"adm_refadd_{kind}"
        )


def statusIndex(isActive):
    return 0 if isActive else 1


@st.dialog("Add crop", width="medium")
def addCropDialog(current_user):
    st.markdown(
        '<div class="hl-release-head"><strong>Add a crop</strong>'
        '<span>New crops are active straight away, so farmers can plan cycles with them.</span></div>',
        unsafe_allow_html=True
    )
    with st.form("admin_add_crop", border=False):
        crop_name = st.text_input("Crop name", placeholder="e.g. Tomatoes")
        submitted = st.form_submit_button("Add crop", type="primary", icon=":material/check:", width="stretch")
    if submitted:
        name = crop_name.strip()
        if not name or len(name) > 100:
            st.warning("Enter a crop name of 1-100 characters.")
        elif changeData(
            "INSERT INTO Crops (cropName, isActive) VALUES (%s, 1)", (name,)
        ):
            logActivity(current_user["userID"], "admin_crop_created", f"Added crop {name}")
            st.session_state["adminCropMessage"] = "Crop added successfully."
            st.rerun()


@st.dialog("Edit crop", width="medium")
def editCropDialog(crop, current_user):
    from html import escape

    cropID = int(crop.cropID)
    st.markdown(
        f'<div class="hl-release-head"><strong>{escape(str(crop.cropName))} · Crop #{cropID}</strong>'
        '<span>Inactive crops stay in past records but can\'t be picked for new entries.</span></div>',
        unsafe_allow_html=True
    )
    with st.form(f"admin_edit_crop_{cropID}", border=False):
        columns = st.columns(2)
        updatedName = columns[0].text_input("Crop name", value=str(crop.cropName))
        activeLabel = columns[1].selectbox(
            "Status", ["Active", "Inactive"], index=statusIndex(isActiveFlag(crop.isActive))
        )
        editSubmitted = st.form_submit_button("Save changes", type="primary", icon=":material/check:", width="stretch")
    if editSubmitted:
        success, message = changeReferenceCrop(
            cropID, updatedName, activeLabel == "Active", current_user["userID"]
        )
        if success:
            st.session_state["adminCropMessage"] = message
            st.rerun()
        else:
            st.warning(message)


@st.dialog("Add market", width="medium")
def addMarketDialog(current_user):
    st.markdown(
        '<div class="hl-release-head"><strong>Add a market</strong>'
        '<span>Buyers can send requests to a new market straight away.</span></div>',
        unsafe_allow_html=True
    )
    with st.form("admin_add_market", border=False):
        columns = st.columns(2)
        market_name = columns[0].text_input("Market name", placeholder="e.g. Al Ain Market")
        market_location = columns[1].text_input("Location", placeholder="e.g. Al Ain")
        submitted = st.form_submit_button("Add market", type="primary", icon=":material/check:", width="stretch")
    if submitted:
        name = market_name.strip()
        place = market_location.strip()
        if not name or len(name) > 100 or not place or len(place) > 255:
            st.warning("Enter a market name (1-100) and location (1-255).")
        elif changeData(
            "INSERT INTO Markets (marketName, location, isActive) VALUES (%s, %s, 1)",
            (name, place)
        ):
            logActivity(current_user["userID"], "admin_market_created", f"Added market {name}")
            st.session_state["adminMarketMessage"] = "Market added successfully."
            st.rerun()


@st.dialog("Edit market", width="medium")
def editMarketDialog(market, current_user):
    from html import escape

    marketID = int(market.marketID)
    st.markdown(
        f'<div class="hl-release-head"><strong>{escape(str(market.marketName))} · Market #{marketID}</strong>'
        '<span>Inactive markets stay on earlier requests but can\'t be chosen for new ones.</span></div>',
        unsafe_allow_html=True
    )
    with st.form(f"admin_edit_market_{marketID}", border=False):
        columns = st.columns(2)
        updatedName = columns[0].text_input("Market name", value=str(market.marketName))
        updatedLocation = columns[1].text_input("Location", value=str(market.location))
        activeLabel = st.selectbox(
            "Status", ["Active", "Inactive"], index=statusIndex(isActiveFlag(market.isActive))
        )
        editSubmitted = st.form_submit_button("Save changes", type="primary", icon=":material/check:", width="stretch")
    if editSubmitted:
        success, message = changeReferenceMarket(
            marketID, updatedName, updatedLocation,
            activeLabel == "Active", current_user["userID"]
        )
        if success:
            st.session_state["adminMarketMessage"] = message
            st.rerun()
        else:
            st.warning(message)


@st.dialog("Add unit", width="medium")
def addUnitDialog(current_user):
    st.markdown(
        '<div class="hl-release-head"><strong>Add a unit</strong>'
        '<span>Farmers and buyers can pick it for new harvests and requests.</span></div>',
        unsafe_allow_html=True
    )
    with st.form("admin_add_unit", border=False):
        newUnit = st.text_input("Unit", placeholder="e.g. crate")
        addUnitSubmitted = st.form_submit_button("Add unit", type="primary", icon=":material/check:", width="stretch")
    if addUnitSubmitted:
        success, message = addReferenceUnit(newUnit, current_user["userID"])
        if success:
            st.session_state["adminUnitMessage"] = message
            st.rerun()
        else:
            st.warning(message)


@st.dialog("Change unit status", width="medium")
def editUnitDialog(unit, current_user):
    from html import escape

    unitName = str(unit.unitName)
    currentlyActive = isActiveFlag(unit.isActive)
    st.markdown(
        f'<div class="hl-release-head"><strong>{escape(unitName)}</strong>'
        f'<span>Currently {"active" if currentlyActive else "inactive"}. Inactive units stay in older records '
        'but can\'t be picked for new entries.</span></div>',
        unsafe_allow_html=True
    )
    with st.form("admin_change_unit_status", border=False):
        desiredStatus = st.selectbox(
            "Status", ["Active", "Inactive"], index=statusIndex(currentlyActive)
        )
        statusSubmitted = st.form_submit_button("Save status", type="primary", icon=":material/check:", width="stretch")
    if statusSubmitted:
        success, message = setReferenceUnitActive(
            unitName, desiredStatus == "Active", current_user["userID"]
        )
        if success:
            st.session_state["adminUnitMessage"] = message
            st.rerun()
        else:
            st.warning(message)


def showAdminReferenceData(current_user):
    st.header("Reference data")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator reference data")
        return

    crop_tab, market_tab, unit_tab = st.tabs(
        ["Crops", "Markets", "Units"]
    )

    with crop_tab:
        if "adminCropMessage" in st.session_state:
            st.success(st.session_state.pop("adminCropMessage"))

        crops = selectData("SELECT cropID, cropName, isActive FROM Crops ORDER BY cropName")
        if referenceHead(
            "crop", "Crops", len(crops),
            "Inactive crops stay in past records but can't be picked for new entries.",
            "Add crop"
        ):
            addCropDialog(current_user)

        if crops.empty:
            st.info("No crops yet.")
        else:
            with st.container(key="adm_reflist_crop", gap=None):
                for crop in crops.itertuples(index=False):
                    cropID = int(crop.cropID)
                    with st.container(horizontal=True, vertical_alignment="center", key=f"adm_urow_crop_{cropID}"):
                        st.markdown(
                            referenceRowHtml(crop.cropName, f"Crop #{cropID}", isActiveFlag(crop.isActive)),
                            unsafe_allow_html=True
                        )
                        if st.button("Edit", key=f"adm_edit_crop_{cropID}", icon=":material/edit:"):
                            editCropDialog(crop, current_user)

    with market_tab:
        if "adminMarketMessage" in st.session_state:
            st.success(st.session_state.pop("adminMarketMessage"))

        markets = selectData(
            "SELECT marketID, marketName, location, isActive FROM Markets ORDER BY marketName"
        )
        if referenceHead(
            "market", "Markets", len(markets),
            "Inactive markets stay on earlier requests but can't be chosen for new ones.",
            "Add market"
        ):
            addMarketDialog(current_user)

        if markets.empty:
            st.info("No markets yet.")
        else:
            with st.container(key="adm_reflist_market", gap=None):
                for market in markets.itertuples(index=False):
                    marketID = int(market.marketID)
                    with st.container(horizontal=True, vertical_alignment="center", key=f"adm_urow_market_{marketID}"):
                        st.markdown(
                            referenceRowHtml(
                                market.marketName,
                                f"{market.location} · Market #{marketID}",
                                isActiveFlag(market.isActive)
                            ),
                            unsafe_allow_html=True
                        )
                        if st.button("Edit", key=f"adm_edit_market_{marketID}", icon=":material/edit:"):
                            editMarketDialog(market, current_user)

    with unit_tab:
        if "adminUnitMessage" in st.session_state:
            st.success(st.session_state.pop("adminUnitMessage"))

        unitData = selectData(
            "SELECT unitName, isActive FROM Units ORDER BY unitName"
        )
        if referenceHead(
            "unit", "Units", len(unitData),
            "Inactive units stay in older records but can't be picked for new entries.",
            "Add unit"
        ):
            addUnitDialog(current_user)

        if unitData.empty:
            st.info("No units yet.")
        else:
            with st.container(key="adm_reflist_unit", gap=None):
                for index, unit in enumerate(unitData.itertuples(index=False)):
                    with st.container(horizontal=True, vertical_alignment="center", key=f"adm_urow_unit_{index}"):
                        st.markdown(
                            referenceRowHtml(
                                unit.unitName,
                                UNIT_NAMES.get(str(unit.unitName).lower(), "Custom unit"),
                                isActiveFlag(unit.isActive)
                            ),
                            unsafe_allow_html=True
                        )
                        if st.button("Change status", key=f"adm_edit_unit_{index}", icon=":material/toggle_on:"):
                            editUnitDialog(unit, current_user)


RECORD_TYPES = {
    "Farms": ("Farms", "Every farm and where it is."),
    "Farm ownership": ("Farm owners", "Which farmer owns each farm."),
    "CropCycle": ("Crop cycles", "What each farm is growing, and when it expects to harvest."),
    "HarvestLots": ("Harvest lots", "Each harvested batch, with its amount and sell-by date."),
    "MarketRequests": ("Requests", "Produce that market buyers have asked for."),
    "Allocations": ("Allocations", "Stock that farmers have reserved against requests."),
    "StockMovements": ("Stock movements", "Every change to lot stock: harvests, reservations, deliveries and losses."),
    "DeliveryReceipts": ("Deliveries", "Deliveries confirmed by buyers, with any shortfall."),
    "Request status history": ("Request history", "Each time a request changed status."),
    "Security activity": ("Activity log", "Sign-ins, denied access and admin changes."),
}

RECORD_COLUMNS = {
    "farmID": "Farm #", "farmName": "Farm", "location": "Location", "ownedBy": "Owner",
    "cycleID": "Cycle #", "cropID": "Crop #", "expectedUnit": "Unit", "expectedYield": "Expected yield",
    "expectedHarvestDate": "Expected harvest", "status": "Status",
    "lotID": "Lot #", "quantityHarvested": "Harvested", "unit": "Unit",
    "harvestDate": "Harvested on", "sellByDate": "Sell by",
    "requestID": "Request #", "marketID": "Market #", "quantityRequested": "Requested",
    "requestDate": "Requested on", "reqStatus": "Status", "requestedBy": "Requested by",
    "allocationID": "Allocation #", "quantityAllocated": "Allocated", "allocationDate": "Allocated on",
    "allocStatus": "Status", "allocatedByName": "Allocated by",
    "movementID": "Movement #", "movementType": "Type", "quantity": "Quantity",
    "dateOccurred": "Date", "reason": "Reason", "performedByName": "Performed by",
    "receiptID": "Receipt #", "expectedQuantity": "Expected", "receivedQuantity": "Received",
    "discrepancyQuantity": "Short by", "deliveryDate": "Delivered on", "confirmedByName": "Confirmed by",
    "historyID": "History #", "oldStatus": "From", "newStatus": "To", "changedAt": "Changed on",
    "changedByName": "Changed by", "activityID": "Activity #", "actionType": "Activity",
    "actionDate": "When", "details": "Details",
}

RECORD_PERSON_IDS = ["userID", "allocatedBy", "performedBy", "confirmedBy", "changedBy"]
RECORD_DATES = {"expectedHarvestDate", "harvestDate", "sellByDate"}
RECORD_DATETIMES = {"requestDate", "allocationDate", "dateOccurred", "deliveryDate", "changedAt", "actionDate"}

MOVEMENT_LABELS = {
    "added": "Harvest added",
    "reserved": "Reserved",
    "delivered": "Delivered",
    "canceled": "Released",
    "loss": "Loss",
    "correction_added": "Correction",
}


def readableRecords(table, records):
    records = records.copy()
    personColumns = [
        column for column in RECORD_PERSON_IDS
        if column in records.columns
        and any(name in records.columns for name in ("requestedBy", "allocatedByName", "performedByName",
                                                     "confirmedByName", "changedByName"))
    ]
    records = records.drop(columns=personColumns)

    def formatDates(column, pattern):
        parsed = pd.to_datetime(records[column], errors="coerce")
        records[column] = [
            None if pd.isna(value) else f"{value.day} {value.strftime(pattern)}" for value in parsed
        ]

    for column in records.columns:
        if column in RECORD_DATES:
            formatDates(column, "%b %Y")
        elif column in RECORD_DATETIMES:
            formatDates(column, "%b %Y, %H:%M")

    if "status" in records.columns:
        activeText = "Growing" if table == "CropCycle" else "Active"
        inactiveText = "Finished" if table == "CropCycle" else "Inactive"
        records["status"] = records["status"].map(
            lambda value: activeText if isActiveFlag(value) else inactiveText
        )
    for column in ("reqStatus", "oldStatus", "newStatus"):
        if column in records.columns:
            records[column] = records[column].map(
                lambda value: value if value is None or pd.isna(value)
                else REQUEST_STATUS_LABELS.get(str(value), (str(value),))[0]
            )
    if "allocStatus" in records.columns:
        records["allocStatus"] = records["allocStatus"].map(
            lambda value: ALLOCATION_STATUS.get(str(value), (str(value),))[0]
        )
    if "movementType" in records.columns:
        records["movementType"] = records["movementType"].map(
            lambda value: MOVEMENT_LABELS.get(str(value), str(value).replace("_", " ").capitalize())
        )
    if "actionType" in records.columns:
        records["actionType"] = records["actionType"].map(activityLabel)

    return records.rename(columns=RECORD_COLUMNS)


def keepRecordType():
    if st.session_state.get("adm_rec_type") is None:
        st.session_state["adm_rec_type"] = st.session_state.get("adm_rec_type_last", "Farms")
    st.session_state["adm_rec_type_last"] = st.session_state["adm_rec_type"]


def showAdminOperations(current_user):
    st.header("Records")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator operational records")
        return

    table = st.pills(
        "Record type",
        list(RECORD_TYPES),
        format_func=lambda key: RECORD_TYPES[key][0],
        default="Farms",
        key="adm_rec_type",
        on_change=keepRecordType,
        label_visibility="collapsed"
    ) or "Farms"

    allowed_queries = {
        "Farms": "SELECT * FROM Farms ORDER BY farmID DESC",
        "Farm ownership": """SELECT f.farmID, f.farmName, f.location, CONCAT(u.fname, ' ', u.lname) AS ownedBy
                           FROM Farms f, OwnedBy o, Users u
                           WHERE f.farmID = o.farmID AND o.userID = u.userID
                           ORDER BY f.farmName""",
        "CropCycle": "SELECT * FROM CropCycle ORDER BY cycleID DESC",
        "HarvestLots": "SELECT * FROM HarvestLots ORDER BY lotID DESC",
        "MarketRequests": """SELECT mr.*, CONCAT(u.fname, ' ', u.lname) AS requestedBy
                            FROM MarketRequests mr, Users u
                            WHERE mr.userID = u.userID
                            ORDER BY mr.requestID DESC""",
        "Allocations": """SELECT a.*, CONCAT(u.fname, ' ', u.lname) AS allocatedByName
                         FROM Allocations a, Users u
                         WHERE a.allocatedBy = u.userID
                         ORDER BY a.allocationID DESC""",
        "StockMovements": """SELECT sm.*, CONCAT(u.fname, ' ', u.lname) AS performedByName
                            FROM StockMovements sm, Users u
                            WHERE sm.performedBy = u.userID
                            ORDER BY sm.movementID DESC""",
        "DeliveryReceipts": """SELECT dr.*, CONCAT(u.fname, ' ', u.lname) AS confirmedByName
                              FROM DeliveryReceipts dr, Users u
                              WHERE dr.confirmedBy = u.userID
                              ORDER BY dr.receiptID DESC""",
        "Request status history": """SELECT rh.*, CONCAT(u.fname, ' ', u.lname) AS changedByName
                                   FROM RequestStatusHistory rh, Users u
                                   WHERE rh.changedBy = u.userID
                                   ORDER BY rh.changedAt DESC""",
        "Security activity": """SELECT al.*, CONCAT(u.fname, ' ', u.lname) AS performedByName
                              FROM UserActivityLog al, Users u
                              WHERE al.userID = u.userID
                              ORDER BY al.actionDate DESC""",
    }

    from html import escape

    records = selectData(allowed_queries[table])
    label, note = RECORD_TYPES[table]
    st.markdown(
        f'<div class="hl-ref-head hl-rec-head"><strong>{escape(label)} ({len(records)})</strong>'
        f'<span>{escape(note)} Read-only.</span></div>',
        unsafe_allow_html=True
    )
    if records.empty:
        st.info("There are no records of this type yet.")
    else:
        showTable(readableRecords(table, records), use_container_width=True, hide_index=True)


# -----------------------------------------------------------------------------
# Read-only LangChain assistant
# -----------------------------------------------------------------------------
def getLangChainModel():
    # Use LangChain to connect to Gemini with the existing API key.
    apiKey = os.getenv("GEMINI_API_KEY")

    if not apiKey:
        raise ValueError("The database assistant is not configured.")

    return ChatGoogleGenerativeAI(
        model=getModelName(),
        google_api_key=apiKey,
        temperature=0
    )


def getLangChainText(response):
    # Extract the text returned by the LangChain model.
    if isinstance(response.content, str):
        return response.content
    return "".join(
        part.get("text", "")
        for part in response.content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def getModelName():
    return os.getenv(
        "GEMINI_MODEL",
        "gemini-3.5-flash"
    )


def getSchemaText():
    schemaQuery = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """

    schemaData = selectData(
        schemaQuery,
        show_error=False
    )

    if schemaData.empty:
        return ""

    groupedTables = {}

    for _, column in schemaData.iterrows():
        tableName = column["TABLE_NAME"]
        columnName = column["COLUMN_NAME"]
        dataType = column["DATA_TYPE"]

        if tableName not in groupedTables:
            groupedTables[tableName] = []

        groupedTables[tableName].append(
            f"{columnName} ({dataType})"
        )

    schemaParts = []

    for tableName, columns in groupedTables.items():
        schemaParts.append(
            f"Table: {tableName}\n"
            f"Columns: {', '.join(columns)}"
        )

    return "\n\n".join(schemaParts)


def extractSQL(rawText):
    rawText = rawText.strip()

    sqlBlock = re.search(
        r"```sql\s*(.*?)```",
        rawText,
        re.IGNORECASE | re.DOTALL
    )

    if sqlBlock:
        return sqlBlock.group(1).strip()

    codeBlock = re.search(
        r"```\s*(.*?)```",
        rawText,
        re.DOTALL
    )

    if codeBlock:
        return codeBlock.group(1).strip()

    return rawText


# Only the assistant uses these restricted views of the database.
# Normal app pages keep their existing role and transaction checks.
ASSISTANT_TABLES = {
    "users", "farms", "ownedby", "crops", "cropcycle", "harvestlots",
    "harvestlotimages", "markets", "marketrequests", "requeststatushistory",
    "allocations", "stockmovements", "deliveryreceipts", "useractivitylog", "units"
}


def assistantScope(tableName, role, userID):
    # These are fixed server-written queries, not instructions from Gemini.
    ownFarms = f"farmID IN (SELECT farmID FROM OwnedBy WHERE userID = {userID})"
    ownCycles = f"cycleID IN (SELECT cycleID FROM CropCycle WHERE {ownFarms})"
    ownLots = f"lotID IN (SELECT lotID FROM HarvestLots WHERE {ownCycles})"
    ownAllocations = f"allocationID IN (SELECT allocationID FROM Allocations WHERE {ownLots})"
    ownRequests = f"requestID IN (SELECT requestID FROM MarketRequests WHERE userID = {userID})"
    table = tableName.lower()

    if role == "admin":
        # Even administrators cannot retrieve password hashes through the assistant.
        if table == "users":
            return "SELECT userID, fname, lname, role, email FROM Users"
        return f"SELECT * FROM `{tableName}`"

    if role == "farmer":
        filters = {
            "farms": ownFarms,
            "ownedby": f"userID = {userID}",
            "cropcycle": ownFarms,
            "harvestlots": ownCycles,
            "harvestlotimages": ownLots,
            "stockmovements": ownLots,
            "allocations": ownLots,
            "deliveryreceipts": ownAllocations,
            "marketrequests": "reqStatus IN ('pending', 'partiallyFulfilled')",
            "requeststatushistory": (
                "requestID IN (SELECT requestID FROM Allocations WHERE " + ownLots + ")"
            ),
        }
        if table in {"users", "useractivitylog"}:
            return None
        if table == "marketrequests":
            # Farmers can allocate open requests, but not read buyer identities.
            return ("SELECT requestID, marketID, cropID, quantityRequested, "
                    "unit, requestDate, reqStatus FROM MarketRequests "
                    "WHERE reqStatus IN ('pending', 'partiallyFulfilled')")
        if table in filters:
            return f"SELECT * FROM `{tableName}` WHERE {filters[table]}"
        if table in {"crops", "markets", "units"}:
            return f"SELECT * FROM `{tableName}`"
        return None

    if role == "user":
        # Public inventory movements expose quantities, never activity details.
        if table == "stockmovements":
            return ("SELECT lotID, movementType, quantity, unit FROM StockMovements "
                    "WHERE lotID IN (SELECT lotID FROM HarvestLots WHERE status = 1)")
        filters = {
            "marketrequests": f"userID = {userID}",
            "allocations": ownRequests,
            "deliveryreceipts": ownRequests,
            "requeststatushistory": ownRequests,
        }
        if table in filters:
            return f"SELECT * FROM `{tableName}` WHERE {filters[table]}"
        if table in {"farms", "crops", "cropcycle", "harvestlots", "harvestlotimages", "markets", "units"}:
            return f"SELECT * FROM `{tableName}`"
        return None
    return None


def isPrivateAssistantQuestion(question, role):
    # Refuse explicit requests for someone else's private records.
    if role == "admin":
        return False
    question = question.lower()
    aboutOthers = re.search(
        r"\b(other (?:users?|buyers?|farmers?|people)|another (?:user|buyer|farmer)|"
        r"someone else|every (?:user|buyer|farmer)|all (?:users|buyers|farmers))\b",
        question,
    )
    sensitive = re.search(
        r"\b(requests?|receipts?|deliveries|allocations|farms?|stock|"
        r"movements?|history|emails?|passwords?|accounts?|activity)\b",
        question,
    )
    return bool(aboutOthers and sensitive)


def directRequestQuestion(question, currentUser):
    # Common request questions use fixed SQL and do not spend Gemini quota.
    normalized = re.sub(r"[^a-z0-9 ]", " ", question.lower())
    normalized = " ".join(normalized.split())
    role = str(currentUser["role"]).lower()
    userID = int(currentUser["userID"])

    myRequests = re.fullmatch(
        r"(?:show|list|display|what are) (?:me )?my (?:market |produce )?requests?",
        normalized
    )
    if myRequests:
        if role != "user":
            return ("message", "Only market buyers create market requests. "
                    "As a farmer, ask to see pending or open market requests instead.")
        return ("sql", f"""SELECT mr.requestID, m.marketName, c.cropName,
                   mr.quantityRequested, mr.unit, mr.requestDate, mr.reqStatus
            FROM MarketRequests mr, Markets m, Crops c
            WHERE mr.marketID = m.marketID AND mr.cropID = c.cropID
            AND mr.userID = {userID}
            ORDER BY mr.requestDate DESC""")

    statusQuestion = re.fullmatch(
        r"(?:show|list|display|what are) (?:the |all )?"
        r"(pending|open) (?:market |produce )?requests?",
        normalized
    )
    if statusQuestion:
        status = statusQuestion.group(1)
        statusSQL = ("mr.reqStatus = 'pending'" if status == "pending" else
                     "mr.reqStatus IN ('pending', 'partiallyFulfilled')")
        buyerSQL = f" AND mr.userID = {userID}" if role == "user" else ""
        return ("sql", f"""SELECT mr.requestID, m.marketName, c.cropName,
                   mr.quantityRequested, mr.unit, mr.requestDate, mr.reqStatus
            FROM MarketRequests mr, Markets m, Crops c
            WHERE mr.marketID = m.marketID AND mr.cropID = c.cropID
            AND {statusSQL}{buyerSQL}
            ORDER BY mr.requestDate DESC""")
    return None



# Recognize explicit availability dates without relying on model-generated SQL.
def directAvailabilityQuestion(question):
    normalized = " ".join(str(question).strip().lower().split())
    if not re.search(r"\b(?:available|availability)\b", normalized):
        return None
    if not re.search(r"\b(?:harvest lots?|produce|crops?|listings?)\b", normalized):
        return None

    # Supported examples: November 20, 2026; 20 November 2026; 2026-11-20.
    monthNames = (
        "january|february|march|april|may|june|july|august|"
        "september|october|november|december"
    )
    patterns = (
        (rf"\b({monthNames})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b", "%B %d %Y", "month_first"),
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({monthNames})[,]?\s+(\d{{4}})\b", "%d %B %Y", "day_first"),
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", "%Y-%m-%d", "iso"),
    )
    selectedDate = None
    for pattern, dateFormat, order in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            groups = match.groups()
            value = " ".join(groups) if order != "iso" else "-".join(groups)
            try:
                selectedDate = datetime.strptime(value.title() if order != "iso" else value, dateFormat).date()
            except ValueError:
                return ("message", "That date is invalid. Please enter a valid date, such as November 20, 2026.")
            break

    if selectedDate is None:
        return None

    # This is a date-filtered view of the inventory currently recorded in MySQL.
    # It cannot predict future stock movements or reservations.
    stockAvailable = """SUM(CASE
        WHEN sm.movementType IN ('added', 'correction_added', 'canceled') THEN sm.quantity
        WHEN sm.movementType IN ('loss', 'correction_removed', 'reserved') THEN -sm.quantity
        ELSE 0 END)"""
    sql = f"""SELECT hl.lotID, c.cropName, {stockAvailable} AS availableQuantity,
                   hl.unit, hl.harvestDate, hl.sellByDate
            FROM HarvestLots hl, CropCycle cc, Crops c, StockMovements sm
            WHERE hl.cycleID = cc.cycleID AND cc.cropID = c.cropID
              AND sm.lotID = hl.lotID AND hl.status = 1 AND c.isActive = 1
              AND hl.harvestDate <= '{selectedDate.isoformat()}'
              AND (hl.sellByDate IS NULL OR hl.sellByDate >= '{selectedDate.isoformat()}')
            GROUP BY hl.lotID, c.cropName, hl.unit, hl.harvestDate, hl.sellByDate
            HAVING availableQuantity > 0
            ORDER BY c.cropName, hl.lotID"""
    return ("sql", sql, selectedDate)


def formatDirectAvailabilityAnswer(results, selectedDate):
    displayDate = selectedDate.strftime("%B %d, %Y").replace(" 0", " ")
    if results.empty:
        return (f"No active harvest lots match the availability conditions for {displayDate} "
                "using the inventory currently recorded in the database. "
                "Future reservations, deliveries or losses may change availability.")
    count = len(results)
    details = []
    for _, lot in results.iterrows():
        details.append(
            f"Lot {int(lot['lotID'])}: {lot['cropName']} — "
            f"{float(lot['availableQuantity']):,.2f} {lot['unit']} available"
        )
    return (f"Based on current recorded stock, {count} harvest "
            f"lot{'s' if count != 1 else ''} {'meet' if count != 1 else 'meets'} the availability conditions "
            f"for {displayDate}:\n\n" + "\n\n".join(details) +
            "\n\nThis is not a guarantee of future stock: reservations, "
            "deliveries and losses may change these quantities.")

def formatDirectRequestAnswer(results):
    # Describe actual query results without another API request.
    if results.empty:
        return "No matching market requests were found."
    count = len(results)
    return (f"Found {count} market request{'s' if count != 1 else ''}. "
            "See the supporting records for the crop, quantity, unit, market, "
            "request date and status.")


def prepareAssistantSQL(sql, currentUser):
    # Parse every statement; never execute an unchecked model response.
    try:
        statements = parse(sql, read="mysql", error_level="RAISE")
    except (ParseError, ValueError, TypeError):
        raise ValueError("The assistant generated invalid SQL.") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Only one SELECT query is allowed.")

    query = statements[0]
    if query.args.get("with_") or query.args.get("into") or query.args.get("locks"):
        raise ValueError("This SQL form is not supported.")
    if any(isinstance(node, (exp.Union, exp.Intersect, exp.Except)) for node in query.walk()):
        raise ValueError("Combined SQL statements are not supported.")

    # Block files, delays, user variables, and database metadata.
    blockedFunctions = {
        "SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK",
        "IS_FREE_LOCK", "IS_USED_LOCK", "EXTRACTVALUE", "UPDATEXML"
    }
    for node in query.walk():
        if isinstance(node, exp.Anonymous) and node.name.upper() in blockedFunctions:
            raise ValueError("This SQL function is not allowed.")
        if isinstance(node, (exp.Parameter, exp.Placeholder)):
            raise ValueError("SQL variables and parameters are not allowed.")
        if isinstance(node, exp.Select) and (node.args.get("with_") or node.args.get("into") or node.args.get("locks")):
            raise ValueError("This SQL form is not supported.")

    role = str(currentUser["role"]).lower()
    userID = int(currentUser["userID"])
    if role not in {"admin", "farmer", "user"} or userID <= 0:
        raise ValueError("The signed-in account is not authorized.")

    # Wrap every table, including tables inside nested SELECT statements.
    # This makes a generated WHERE userID <> 2 unable to reveal other buyers.
    tables = list(query.find_all(exp.Table))
    if not tables:
        raise ValueError("The query must use an approved database table.")
    for table in tables:
        tableName = table.name
        if table.db or table.catalog or tableName.lower() not in ASSISTANT_TABLES:
            raise ValueError("The query references a restricted table.")
        scopedSQL = assistantScope(tableName, role, userID)
        if scopedSQL is None:
            raise ValueError("This table is not available to your account.")
        aliasName = table.alias_or_name
        replacement = parse_one(scopedSQL, read="mysql").subquery(alias=aliasName)
        table.replace(replacement)

    # The top-level result cannot exceed 200 records.
    query.limit(200, copy=False)
    return query.sql(dialect="mysql")


def isSafeSelectQuery(sql):
    # Kept for compatibility with any earlier calls.
    try:
        statements = parse(sql, read="mysql", error_level="RAISE")
        return len(statements) == 1 and isinstance(statements[0], exp.Select)
    except (ParseError, ValueError, TypeError):
        return False


def generateSQL(question, schemaText, currentUser):
    model = getLangChainModel()

    userID = int(currentUser["userID"])
    role = currentUser["role"]

    roleRule = ""

    if role == "farmer":
        roleRule = f"""
The signed-in user is farmer userID {userID}.

When retrieving farms, crop cycles, harvest lots, allocations,
stock movements, or images, include the OwnedBy table and restrict
the results using:

OwnedBy.userID = {userID}

The farmer must never see records belonging only to another farmer.
Farmers may see open market requests because they need to allocate
produce to them. For pending or open requests, query MarketRequests,
Markets and Crops, not Users or UserActivityLog. Do not select buyer userID,
name or email; those private fields are not part of the farmer assistant view.
If the farmer says "my market requests", explain that requests are made by buyers.
"""

    elif role == "user":
        roleRule = f"""
The signed-in market buyer has userID {userID}.

The buyer may search active produce from all farms.

When retrieving MarketRequests or DeliveryReceipts belonging to the
buyer, restrict the results using:

MarketRequests.userID = {userID}

The buyer must not see requests or receipts belonging to another buyer.
"""

    elif role == "admin":
        roleRule = """
The signed-in user is an administrator.

Administrators may view operational and reference records, but must
never retrieve passwordHash values.
"""

    prompt = f"""
    You are a MySQL database assistant for a local farm and market
    food-availability system.

    Generate exactly ONE valid MySQL SELECT query that answers the
    user's question.

    GENERAL RULES

    1. Output only the SQL query.
    2. Generate only a read-only SELECT query.
    3. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE,
    CREATE, REPLACE, RENAME, GRANT or REVOKE.
    4. Never select passwordHash.
    4a. Do not request data belonging to another user.
    4b. Avoid SELECT * when a small set of columns is enough.
    5. Use only tables and columns shown in the provided schema.
    6. Use exact table and column names from the schema.
    7. Use comma-separated tables with WHERE conditions.
    8. Include the unit whenever returning a quantity.
    9. Never add or compare quantities with different units.
    10. Limit detailed results to 200 rows.
    11. Use CURRENT_DATE when the user says today, currently or now.
    12. Do not assume that a database status literally uses the words
        "active", "open", "finished" or "history". Translate those words
        using the terminology below.
    13. If the question is ambiguous, unsupported or cannot be answered,
        output exactly:
        NOT_ANSWERABLE

    DATABASE TERMINOLOGY

    USERS AND OWNERSHIP

    - "Farmer", "farm user" or "farm owner" means a Users record whose
    role is 'farmer'.

    - A farmer's farms are determined through OwnedBy:
    Users.userID = OwnedBy.userID
    OwnedBy.farmID = Farms.farmID

    - A farmer owns a crop cycle through:
    CropCycle.farmID = OwnedBy.farmID

    - A farmer owns a harvest lot through:
    HarvestLots.cycleID = CropCycle.cycleID
    CropCycle.farmID = OwnedBy.farmID

    - A farmer's allocation is determined through the allocated lot:
    Allocations.lotID = HarvestLots.lotID
    HarvestLots.cycleID = CropCycle.cycleID
    CropCycle.farmID = OwnedBy.farmID

    - Do not use Allocations.allocatedBy as the only ownership check.

    - "My", "mine" or "belonging to me" must be interpreted according
    to the signed-in user's role and the ownership rules above.

    CROP-CYCLE TERMINOLOGY

    - "Active", "current", "ongoing", "planned" or "upcoming"
    crop cycle means:
    CropCycle.status = 1

    - "Finished", "completed", "inactive", "past" or "crop-cycle
    history" means:
    CropCycle.status = 0

    - Expected harvest quantities come only from CropCycle.

    - Expected harvest is a forecast and is not currently available
    harvested stock.

    HARVEST-LOT TERMINOLOGY

    - An active harvest lot has:
    HarvestLots.status = 1

    - An inactive, depleted, expired or historical harvest lot has:
    HarvestLots.status = 0

    - A harvest lot is currently saleable only when:
    HarvestLots.status = 1
    AND HarvestLots.harvestDate <= CURRENT_DATE
    AND (
        HarvestLots.sellByDate IS NULL
        OR HarvestLots.sellByDate >= CURRENT_DATE
    )

    - "Harvested stock" refers to HarvestLots and StockMovements,
    not CropCycle.expectedYield.

    - One crop cycle may have multiple harvest lots.

    STOCK TERMINOLOGY

    Stock availability must be calculated from StockMovements.

    Movement effects:

    - 'added' increases stock on hand.
    - 'correction_added' increases stock on hand.
    - 'delivered' decreases stock on hand.
    - 'loss' decreases stock on hand.
    - 'correction_removed' decreases stock on hand.
    - 'reserved' increases reserved quantity.
    - 'canceled' releases reserved quantity.
    - 'delivered' also releases the delivered reserved quantity.

    Definitions:

    stockOnHand =
        added
        + correction_added
        - delivered
        - loss
        - correction_removed

    reservedQuantity =
        reserved
        - delivered
        - canceled

    availableQuantity =
        stockOnHand
        - reservedQuantity

    Do not treat quantityHarvested alone as current availability.

    ALLOCATION TERMINOLOGY

    - "Active", "current", "open", "outstanding" or "pending"
    allocation means:
    Allocations.allocStatus IN (
        'reserved',
        'partiallyDelivered'
    )

    - "Past", "closed", "completed" or "allocation history" means:
    Allocations.allocStatus IN (
        'delivered',
        'canceled'
    )

    - Never search for allocStatus = 'active' because 'active' is not
    a stored allocation status.

    - An allocation belongs to a market request through requestID.

    REQUEST TERMINOLOGY

    - An open market request has:
    MarketRequests.reqStatus IN (
        'pending',
        'partiallyFulfilled'
    )

    - A closed request has:
    MarketRequests.reqStatus IN (
        'fullyFulfilled',
        'canceled'
    )

    - A buyer owns a market request when:
    MarketRequests.userID = the signed-in buyer's userID

    - "Pending request" means reqStatus = 'pending'.

    - "Partially fulfilled request" means:
    reqStatus = 'partiallyFulfilled'.

    - "Completed" or "fully fulfilled request" means:
    reqStatus = 'fullyFulfilled'.

    - Never search for reqStatus = 'active'.

    DELIVERY TERMINOLOGY

    - Delivery receipts are stored in DeliveryReceipts.

    - The quantity actually received is receivedQuantity.

    - The expected delivery quantity is expectedQuantity.

    - Delivery discrepancy is discrepancyQuantity.

    - A positive discrepancy means the received quantity was less than
    the expected quantity.

    - Delivery receipts belong to buyers through:
    DeliveryReceipts.requestID = MarketRequests.requestID
    MarketRequests.userID = the signed-in buyer's userID

    NAMES

    - A user's full name must be displayed using:
    CONCAT(Users.fname, ' ', Users.lname)

    - There is no fullname column.

    ROLE AND OWNERSHIP RULES

    {roleRule}

    DATABASE SCHEMA

    {schemaText}

    USER QUESTION

    {question}
    """

    # Send the existing prompt through LangChain.
    response = model.invoke(prompt)

    return extractSQL(getLangChainText(response))


def runAssistantSQL(sql):
    connection = None
    cursor = None
    try:
        connection = getConnection()
        cursor = connection.cursor()
        # A read-only transaction protects against write-capable statements.
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(sql)
        rows = cursor.fetchmany(200)
        columns = [column[0] for column in cursor.description]
        return pd.DataFrame(rows, columns=columns)
    except Exception as error:
        print("Assistant query error:", error)
        raise
    finally:
        if connection:
            connection.rollback()
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def summarizeAssistantAnswer(question, sql, results):
    model = getLangChainModel()

    limitedResults = results.head(200)

    prompt = f"""
You are a helpful database course assistant.

Write a short, clear answer using only the supplied query results.

Rules:
1. Do not invent information.
2. Always include units for quantities.
3. Keep expected harvest forecasts separate from available stock.
4. If there are no rows, clearly say that no matching records were found.
5. Do not mention internal database implementation unless needed.
6. Do not include internal model metadata.

Question:
{question}

SQL:
{sql}

Results:
{limitedResults.to_dict(orient="records")}
"""

    # Send the existing prompt through LangChain.
    response = model.invoke(prompt)

    return getLangChainText(response).strip()

FITTON_SUGGESTIONS = {
    "farmer": [
        "Which crops are currently available?",
        "Which harvest lots have no available stock?",
        "What are the active expected harvests?",
        "Show pending market requests",
        "Show delivery discrepancies",
    ],
    "user": [
        "Which crops are currently available?",
        "Show my requests",
        "Show pending market requests",
        "Show delivery discrepancies",
    ],
    "admin": [
        "Which crops are currently available?",
        "Which harvest lots have no available stock?",
        "Show pending market requests",
        "Show delivery discrepancies",
    ],
}

# A fittonia (nerve plant) leaf: pale veins over a green leaf.
FITTON_LEAF = (
    '<path d="M12 2.6C17.6 5.1 20.1 9.6 19.1 14.6c-.9 4.3-4.2 6.7-7.1 6.9-2.9-.2-6.2-2.6-7.1-6.9C3.9 9.6 6.4 5.1 12 2.6z" '
    'fill="#9cc46a"/>'
    '<path d="M12 5.2V21M12 9 8.3 6.9M12 9l3.7-2.1M12 12.6 6.6 10M12 12.6l5.4-2.6M12 16.2l-5.3-2M12 16.2l5.3-2'
    'M8.3 6.9 6.6 10l.1 4.2M15.7 6.9l1.7 3.1-.1 4.2M9.4 11.4l.1 3.6M14.6 11.4l-.1 3.6" '
    'fill="none" stroke="#f5f3ea" stroke-width="1.05" stroke-linecap="round" stroke-linejoin="round"/>'
)


def fittonIcon(size=24, tile=False):
    background = '<rect width="24" height="24" rx="6.5" fill="#2f6b47"/>' if tile else ""
    leaf = f'<g transform="translate(2.4 2.2) scale(.8)">{FITTON_LEAF}</g>' if tile else FITTON_LEAF
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size}" height="{size}" '
        f'aria-hidden="true">{background}{leaf}</svg>'
    )


FITTON_AVATAR = fittonIcon(64, tile=True)


def answerFittonQuestion(question, current_user, onStep=lambda label: None):
    # Runs the same checks as before and returns the reply instead of drawing it.
    def reply(kind, text, results=None, sql=None):
        return {"role": "assistant", "kind": kind, "text": text, "results": results, "sql": sql}

    if not question.strip():
        return reply("note", "Enter a question first.")

    if isPrivateAssistantQuestion(question, current_user["role"]):
        logActivity(current_user["userID"], "assistant_access_denied", "Private data request blocked")
        return reply("blocked", "Access denied. You cannot view other users' private records.")

    # Fixed, date-specific availability queries avoid ambiguous model output.
    directAvailability = directAvailabilityQuestion(question)
    if directAvailability and directAvailability[0] == "message":
        return reply("info", directAvailability[1])

    # Handle common request questions without calling the model.
    directRequest = directRequestQuestion(question, current_user)
    if directRequest and directRequest[0] == "message":
        return reply("info", directRequest[1])

    try:
        onStep("Reading the database structure")
        schemaText = getSchemaText()

        if not schemaText:
            return reply("note", "The database structure could not be read.")

        if directAvailability and directAvailability[0] == "sql":
            sql = directAvailability[1]
        elif directRequest and directRequest[0] == "sql":
            sql = directRequest[1]
        else:
            onStep("Writing a query for your question")
            sql = generateSQL(question, schemaText, current_user)

        if sql == "NOT_ANSWERABLE":
            return reply("info", "This question cannot be answered clearly using the current database.")

        onStep("Checking the query is read-only and allowed")
        try:
            safeSQL = prepareAssistantSQL(sql, current_user)
        except ValueError as error:
            logActivity(current_user["userID"], "assistant_query_blocked", "SQL authorization check failed")
            return reply("blocked", f"The generated query was blocked: {error}")

        onStep("Fetching the records")
        results = runAssistantSQL(safeSQL)

        if directAvailability and directAvailability[0] == "sql":
            answer = formatDirectAvailabilityAnswer(results, directAvailability[2])
        elif directRequest:
            answer = formatDirectRequestAnswer(results)
        else:
            onStep("Summarising the answer")
            try:
                answer = summarizeAssistantAnswer(question, safeSQL, results)
            except Exception as error:
                # Keep supporting SQL and rows visible if Gemini's quota runs out.
                if "RESOURCE_EXHAUSTED" not in str(error) and "429" not in str(error):
                    raise
                answer = ("The records were retrieved, but Gemini's API quota "
                          "prevented an automatic summary. Review the supporting "
                          "records below.")

        return reply("answer", answer, results, safeSQL)

    except ValueError as error:
        return reply("note", str(error))

    except SQLAlchemyError as error:
        print("Assistant database error:", error)
        return reply("error", "The database assistant could not retrieve the records.")

    except Exception as error:
        print("Assistant error:", error)
        return reply("error", "The assistant could not answer this question right now.")


def showFittonMessage(message, key):
    from html import escape

    if message["role"] == "user":
        with st.chat_message("user"):
            st.markdown(
                f'<div class="hl-fitton-question">{escape(message["text"])}</div>',
                unsafe_allow_html=True
            )
        return

    with st.chat_message("assistant", avatar=FITTON_AVATAR):
        if message["kind"] in ("blocked", "error", "note"):
            icon = {"blocked": "lock", "error": "error", "note": "info"}[message["kind"]]
            st.markdown(
                f'<div class="hl-fitton-note hl-fitton-note-{message["kind"]}">'
                f'<span class="material-symbols-rounded" aria-hidden="true">{icon}</span>'
                f'<span>{escape(message["text"])}</span></div>',
                unsafe_allow_html=True
            )
            return

        st.markdown(message["text"])

        if message["kind"] != "answer":
            return

        results = message["results"]
        count = 0 if results is None else len(results)
        with st.expander(
            f"Records ({count})",
            icon=":material/table_rows:",
            key=f"{key}_records"
        ):
            if results is None or results.empty:
                st.info("No matching records were found.")
            else:
                showTable(results)

        with st.expander(
            "SQL used",
            icon=":material/code:",
            key=f"{key}_sql"
        ):
            st.code(message["sql"], language="sql")


def askFitton(question):
    st.session_state["fitton_pending"] = question


def showFittonThread(history, current_user, prefix):
    from html import escape

    for index, message in enumerate(history):
        showFittonMessage(message, f"{prefix}_msg_{index}")

        answered = index + 1 < len(history) and history[index + 1]["role"] == "assistant"
        if message["role"] != "user" or answered:
            continue

        with st.chat_message("assistant", avatar=FITTON_AVATAR):
            progress = st.empty()

            def showStep(label):
                progress.markdown(
                    f'<div class="hl-fitton-thinking"><span class="hl-fitton-dots">'
                    f'<i></i><i></i><i></i></span>{escape(label)}…</div>',
                    unsafe_allow_html=True
                )

            showStep("Checking your question")
            reply = answerFittonQuestion(message["text"], current_user, showStep)
            progress.empty()

        history.insert(index + 1, reply)
        st.rerun()


def openFittonPage(role):
    st.session_state[f"hl_nav_{role}"] = "Assistant"


def showFittonLauncher(current_user):
    from html import escape

    history = st.session_state.setdefault("fitton_chat", [])

    with st.container(key="fitton_launcher"):
        launcher = st.popover(
            "FittonAI",
            help="Ask FittonAI",
            key="fitton_popover",
            on_change="rerun"
        )

    if not launcher.open:
        return

    with launcher, st.container(key="fitton_mini_panel"):
        with st.container(horizontal=True, vertical_alignment="center", gap="small", key="fitton_mini_top"):
            st.markdown(
                f'<div class="hl-fitton-mini-head"><span class="hl-fitton-logo">{fittonIcon(22)}</span>'
                '<div><strong>FittonAI</strong><span>Read-only · your records only</span></div></div>',
                unsafe_allow_html=True,
                width="stretch"
            )
            if history:
                st.button(
                    "",
                    icon=":material/add_comment:",
                    help="New chat",
                    key="fitton_mini_new",
                    type="tertiary",
                    on_click=history.clear
                )
            st.button(
                "",
                icon=":material/open_in_full:",
                help="Open the full FittonAI page",
                key="fitton_mini_full",
                type="tertiary",
                on_click=openFittonPage,
                args=(current_user["role"],)
            )

        thread = st.container(height=380, key="fitton_mini_thread")
        question = st.chat_input("Ask FittonAI a question", key="fitton_mini_input")
        question = question or st.session_state.pop("fitton_pending", None)

        if question:
            history.append({"role": "user", "text": question})

        with thread:
            if history:
                showFittonThread(history, current_user, "fitton_mini")
                return

            firstName = str(current_user.get("fname") or current_user["fullname"]).split()[0]
            st.markdown(
                f'<div class="hl-fitton-mini-welcome"><h3>Hi {escape(firstName)}, ask me anything</h3>'
                '<p>About your crops, stock, requests or deliveries.</p></div>',
                unsafe_allow_html=True
            )
            suggestions = FITTON_SUGGESTIONS.get(current_user["role"], FITTON_SUGGESTIONS["user"])
            with st.container(key="fitton_mini_chips"):
                for index, suggestion in enumerate(suggestions[:4]):
                    st.button(
                        suggestion,
                        key=f"fitton_mini_chip_{index}",
                        on_click=askFitton,
                        args=(suggestion,)
                    )


def showAssistant(current_user):
    from html import escape

    history = st.session_state.setdefault("fitton_chat", [])

    titleCol, actionCol = st.columns([0.75, 0.25], vertical_alignment="center")
    with titleCol:
        st.markdown(
            f'<div class="hl-fitton-head"><span class="hl-fitton-logo">{fittonIcon(32)}</span>'
            '<div><h1>FittonAI</h1><span class="hl-fitton-badge">'
            '<span class="material-symbols-rounded" aria-hidden="true">lock</span>'
            'Read-only · only sees records you\'re allowed to see</span></div></div>',
            unsafe_allow_html=True
        )
    with actionCol:
        with st.container(horizontal=True, horizontal_alignment="right"):
            if history and st.button(
                "New chat",
                icon=":material/add_comment:",
                key="fitton_new_chat"
            ):
                history.clear()
                st.rerun()

    question = st.chat_input(
        "Ask FittonAI about your crops, stock, requests or deliveries",
        key="fitton_input"
    )
    question = question or st.session_state.pop("fitton_pending", None)

    # Queue the question first so a new one sent mid-answer is not lost.
    if question:
        history.append({"role": "user", "text": question})

    if not history:
        firstName = str(current_user.get("fname") or current_user["fullname"]).split()[0]
        st.markdown(
            f'<div class="hl-fitton-welcome"><span class="hl-fitton-eyebrow">Your farm assistant</span>'
            f'<h2>Hi {escape(firstName)}, what would you like to know?</h2>'
            '<p>Ask a question in plain words. FittonAI looks up the answer in HarvestLink '
            'and shows the records and query it used.</p></div>',
            unsafe_allow_html=True
        )
        suggestions = FITTON_SUGGESTIONS.get(current_user["role"], FITTON_SUGGESTIONS["user"])
        with st.container(key="fitton_chips", horizontal=True, horizontal_alignment="center"):
            for index, suggestion in enumerate(suggestions):
                st.button(
                    suggestion,
                    key=f"fitton_chip_{index}",
                    on_click=askFitton,
                    args=(suggestion,)
                )
        return

    showFittonThread(history, current_user, "fitton")


# -----------------------------------------------------------------------------
# Application setup and navigation
# -----------------------------------------------------------------------------


def main():
    connectionTest = selectData(
        "SELECT 1 AS connected",
        show_error=False
    )

    if connectionTest.empty:
        st.error(
            "Could not connect to the database. "
            "Check the DB settings in your .env file."
        )
        return

    if "currentUser" not in st.session_state:
        st.session_state.currentUser = None

    if st.session_state.currentUser is None:
        showAuthentication()
        return

    try:
        currentUser = refreshSignedInUser(st.session_state.currentUser)
    except Exception as error:
        print("Session refresh error:", error)
        st.error("Your account could not be checked right now. Please try again.")
        return

    if currentUser is None:
        # The account no longer exists, so end this session.
        for key in ("currentUser", "sessionStartedAt", "selectedProduceLot", "fitton_chat", "fitton_pending"):
            st.session_state.pop(key, None)
        st.session_state.accountRemovedMessage = True
        st.rerun()

    if currentUser["role"] != st.session_state.currentUser["role"]:
        # Drop state that belonged to the old role's pages.
        for key in ("selectedProduceLot", "fitton_chat", "fitton_pending"):
            st.session_state.pop(key, None)
        newRole = {"farmer": "Farmer", "user": "Market buyer", "admin": "Administrator"}.get(
            currentUser["role"], currentUser["role"]
        )
        st.toast(f"Your role was changed to {newRole}, so your pages have been updated.")

    st.session_state.currentUser = currentUser
    role = currentUser["role"]

    # Pages allowed for each role
    rolePages = {
        "farmer": [
            "Dashboard",
            "Farm management",
            "Harvest management",
            "Requests and allocations",
            "Images",
            "Assistant",
        ],

        "user": [
            "Search produce",
            "My requests",
            "Deliveries",
            "Assistant",
        ],

        "admin": [
            "User management",
            "Reference data",
            "Operational records",
            "Assistant",
        ],
    }

    # Shorter labels for the top navigation bar
    navLabels = {
        "Farm management": "Farms",
        "Harvest management": "Harvests",
        "Requests and allocations": "Requests",
        "Search produce": "Shop",
        "User management": "Users",
        "Operational records": "Records",
        "Assistant": "FittonAI",
    }

    # Reject an invalid or unknown role
    if role not in rolePages:
        st.error("Your account does not have a valid role.")
        return

    pages = rolePages[role]
    roleNames = {"farmer": "Farmer", "user": "Market buyer", "admin": "Administrator"}
    initials = "".join(part[:1] for part in str(currentUser["fullname"]).split()[:2]).upper()
    from html import escape

    with st.container(key="hl_topbar"):
        logoCol, navCol, accountCol = st.columns(
            [0.2, 0.62, 0.18], vertical_alignment="center"
        )

        with logoCol:
            showStoreBar()

        with navCol:
            page = st.radio(
                "Navigation",
                pages,
                horizontal=True,
                key=f"hl_nav_{role}",
                format_func=lambda option: navLabels.get(option, option),
                label_visibility="collapsed",
            )

        with accountCol:
            with st.popover(
                str(currentUser["fullname"]),
                icon=":material/account_circle:",
                width="stretch",
            ):
                st.markdown(
                    f'<div class="hl-account-card"><span class="hl-avatar">{escape(initials)}</span>'
                    f'<span class="hl-account"><b>{escape(str(currentUser["fullname"]))}</b>'
                    f'<small>{roleNames.get(role, role.capitalize())} account</small></span></div>',
                    unsafe_allow_html=True
                )
                st.caption(
                    f"Using {os.getenv('DB_NAME', 'DBProjGroup3')} from the .env file"
                )

                if st.button(
                    "Test database connection",
                    icon=":material/database:",
                    width="stretch",
                ):
                    test = selectData("SELECT 1 AS connected")

                    if not test.empty:
                        st.success(
                            f"Connected to {os.getenv('DB_NAME', 'DBProjGroup3')}."
                        )

                signOutClicked = st.button(
                    "Sign out", key="harvestlink_sign_out", icon=":material/logout:",
                    width="stretch"
                )

    if signOutClicked:
        sessionStarted = st.session_state.get("sessionStartedAt")
        details = "User signed out"

        if sessionStarted:
            duration = datetime.now() - sessionStarted
            details = f"User signed out after {str(duration).split('.')[0]}"

        logActivity(currentUser["userID"], "logout", details)
        st.session_state.currentUser = None
        st.session_state.pop("sessionStartedAt", None)
        st.session_state.pop("selectedProduceLot", None)
        st.session_state.pop("fitton_chat", None)
        st.session_state.pop("fitton_pending", None)
        st.session_state.signedOutMessage = True
        st.rerun()

    if page == "Dashboard":
        showDashboard(currentUser)

    elif page == "Farm management":
        showFarmManagement(currentUser)

    elif page == "Harvest management":
        showHarvestManagement(currentUser)

    elif page == "Requests and allocations":
        showFarmerAllocations(currentUser)

    elif page == "Images":
        showImages(currentUser)

    elif page == "Search produce":
        showProduceSearch(currentUser)

    elif page == "My requests":
        showMarketRequests(currentUser)

    elif page == "Deliveries":
        showDeliveries(currentUser)

    elif page == "User management":
        showAdminUserManagement(currentUser)

    elif page == "Reference data":
        showAdminReferenceData(currentUser)

    elif page == "Operational records":
        showAdminOperations(currentUser)

    elif page == "Assistant":
        showAssistant(currentUser)

    if page != "Assistant":
        showFittonLauncher(currentUser)


if __name__ == "__main__":
    main()
