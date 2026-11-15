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
            return False, "That unit already exists. You can reactivate it below."
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
                key="hl_quick_record", on_click=goToPage, args=("Harvest management",)
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
        cropTotals = (
            available.groupby(["cropName", "unit"])
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
        col1, col2, col3 = st.columns(3)
        col4, col5, col6 = st.columns(3)

        selectedCrop = col1.selectbox("Crop", cropNames)
        selectedFarm = col2.selectbox("Farm", farmNames)
        location = col3.text_input("Location or emirate")

        selectedUnit = col4.selectbox(
            "Unit",
            ["All"] + units
        )

        # Pick the day the buyer wants the produce to be available.
        availableOn = col5.date_input(
            "Available on",
            value=date.today(),
            min_value=date.today()
        )

        minimumQuantity = col6.number_input(
            "Minimum available",
            min_value=0.0
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

        if available.empty:
            st.info("No produce matched the selected filters.")
            return

        st.subheader(f"{len(available)} produce listings")

        cardsPerRow = 3

        for start in range(0, len(available), cardsPerRow):
            columns = st.columns(cardsPerRow)
            rows = available.iloc[start:start + cardsPerRow]

            for columnNumber, (_, lot) in enumerate(rows.iterrows()):
                with columns[columnNumber]:
                    with st.container(border=True):
                        imageData = selectData(
                            """SELECT imageName
                               FROM HarvestLotImages
                               WHERE lotID = %s
                               ORDER BY imageNumber
                               LIMIT 1""",
                            (int(lot["lotID"]),)
                        )

                        if not imageData.empty:
                            imageName = str(
                                imageData.iloc[0]["imageName"]
                            )
                            showHarvestImageFile(
                                imageName,
                                compact=True,
                                use_container_width=True
                            )
                        else:
                            st.caption("No image uploaded.")

                        st.subheader(lot["cropName"])
                        st.write(f"**Farm:** {lot['farmName']}")
                        st.write(f"**Location:** {lot['location']}")

                        st.write(
                            f"**Available:** "
                            f"{float(lot['availableQuantity']):,.2f} "
                            f"{lot['unit']}"
                        )

                        st.caption(
                            f"Harvested: {lot['harvestDate']}  \n"
                            f"Sell by: {lot['sellByDate']}"
                        )

                        if st.button(
                            "View listing",
                            key=f"view_listing_{lot['lotID']}",
                            use_container_width=True
                        ):
                            st.session_state.selectedProduceLot = int(
                                lot["lotID"]
                            )
                            st.rerun()

    # Display the selected listing.
    else:
        selectedLotID = st.session_state.selectedProduceLot

        if st.button("← Back to listings"):
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

        imageColumn, detailColumn = st.columns([1.2, 1])

        with imageColumn:
            images = selectData(
                """SELECT imageNumber, imageName
                   FROM HarvestLotImages
                   WHERE lotID = %s
                   ORDER BY imageNumber""",
                (selectedLotID,)
            )

            if images.empty:
                st.info("No images uploaded for this lot.")

            else:
                for _, image in images.iterrows():
                    showHarvestImageFile(
                        image["imageName"],
                        caption=f"{lot['cropName']} — Lot {selectedLotID}",
                        use_container_width=True
                    )

        with detailColumn:
            st.title(lot["cropName"])
            st.write(f"### {lot['farmName']}")
            st.write(f"📍 {lot['location']}")

            st.metric(
                "Available quantity",
                f"{float(availableQuantity):,.2f} {lot['unit']}"
            )

            st.write(f"**Lot number:** {selectedLotID}")
            st.write(f"**Harvest date:** {lot['harvestDate']}")
            st.write(f"**Sell-by date:** {lot['sellByDate']}")

            st.divider()
            st.subheader(f"Request {lot['cropName']}")

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
                    f"{market['marketName']} — "
                    f"{market['location']}"
                )
                marketOptions[label] = int(market["marketID"])

            with st.form("produce_request_form"):
                selectedMarket = st.selectbox(
                    "Delivery market",
                    list(marketOptions.keys())
                )

                requestedQuantity = st.number_input(f"Quantity requested ({lot['unit']})",min_value=0.01, value=1.0)

                st.text_input(
                    "Unit",
                    value=str(lot["unit"]),
                    disabled=True
                )

                submitted = st.form_submit_button(
                    "Submit request",
                    use_container_width=True
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
                        unit=str(lot["unit"])
                    )

                    if created:
                        st.success(message)
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
                        uploaded_file.seek(0)
                        with Image.open(uploaded_file) as source:
                            preview = ImageOps.exif_transpose(source).convert("RGB")
                            preview.thumbnail((480, 480))
                        st.image(preview, width="stretch")
                    except Exception:
                        st.caption("This file can't be previewed.")
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
            uploadedFile.seek(0)
            with Image.open(uploadedFile) as source:
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

def showFarmManagement(current_user):
    st.header("Farm and crop-cycle management")

    if current_user["role"] != "farmer":
        denyAccess(current_user, "Attempted to open farm management")
        return

    userID = int(current_user["userID"])

    if "selectedHarvestCycle" not in st.session_state:
        st.session_state.selectedHarvestCycle = None

    if "newHarvestLotID" not in st.session_state:
        st.session_state.newHarvestLotID = None

    # Display one-time messages after refreshing.
    if "farmManagementMessage" in st.session_state:
        st.success(
            st.session_state.farmManagementMessage
        )

        del st.session_state.farmManagementMessage

    st.write(
        f"Welcome, **{current_user['fname']}** **{current_user['lname']}**"
    )

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

    with st.expander("Add another farm"):
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

    farmOptions = makeOptions(
        farmData,
        "farmID",
        ["farmName", "location"]
    )
    selectedFarmLabel = st.selectbox(
        "Manage farm",
        list(farmOptions.keys())
    )
    farmID = int(farmOptions[selectedFarmLabel])
    selectedFarm = farmData[farmData["farmID"] == farmID].iloc[0]
    farmName = str(selectedFarm["farmName"])
    farmLocation = str(selectedFarm["location"])

    st.subheader(farmName)
    st.caption(f"📍 {farmLocation}")

    # Edit the farmer's farm information.
    with st.expander("Edit farm information"):
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

    # Add a new crop cycle.
    with st.expander("Add a crop cycle"):
        with st.form("add_cycle_form"):
            selectedCropID = st.selectbox(
                "Crop",
                cropIDs,
                format_func=showCrop
            )

            expectedYield = st.number_input(
                "Expected quantity",
                min_value=0.01,
                value=0.01
            )

            expectedUnit = st.selectbox(
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
                "Add crop cycle"
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

                st.rerun()

    cycles = selectData("""SELECT cc.cycleID, cc.cropID, c.cropName,cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
       FROM CropCycle cc, Crops c
       WHERE cc.cropID = c.cropID AND cc.farmID = %s AND cc.status = 1
       ORDER BY cc.expectedHarvestDate""",(farmID,))

    cycleHistory = selectData(
        """SELECT c.cropName, cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
           FROM CropCycle cc, Crops c
           WHERE cc.cropID = c.cropID AND cc.farmID = %s AND cc.status = 0
           ORDER BY cc.expectedHarvestDate DESC""",
        (farmID,)
    )

    with st.expander("Crop-cycle history"):
        if cycleHistory.empty:
            st.info("There are no completed crop cycles for this farm.")
        else:
            showTable(
                pd.DataFrame({
                    "Crop": cycleHistory["cropName"],
                    "Expected yield": [
                        formatQuantity(q, u)
                        for q, u in zip(cycleHistory["expectedYield"], cycleHistory["expectedUnit"])
                    ],
                    "Expected harvest date": cycleHistory["expectedHarvestDate"],
                }),
                width="stretch",
                hide_index=True
            )

    st.subheader("Active crop cycles")
    if cycles.empty:
        st.info("No crop cycles have been entered.")
        return

    # Display cycle rows with a Record harvest button.
    heading1, heading2, heading3, heading4, heading5 = st.columns(
        [2, 2, 2, 1, 1]
    )

    heading1.write("**Crop**")
    heading2.write("**Expected quantity**")
    heading3.write("**Expected harvest date**")
    heading4.write("")
    heading5.write("")
    for _, cycle in cycles.iterrows():
        column1, column2, column3, column4, column5 = st.columns(
            [2, 2, 2, 1, 1]
        )

        cycleID = int(cycle["cycleID"])

        column1.write(cycle["cropName"])

        column2.write(
            f"{float(cycle['expectedYield']):,.2f} "
            f"{cycle['expectedUnit']}"
        )

        column3.write(
            str(cycle["expectedHarvestDate"])
        )

        if column4.button(
            "Record harvest",
            key=f"record_cycle_{cycleID}"
        ):
            st.session_state.selectedHarvestCycle = cycleID
            st.rerun()

        if column5.button(
            "Finish cycle",
            key=f"finish_cycle_{cycleID}"
        ):
            finished, message = finishCropCycle(
                cycleID,
                userID
            )

            if finished:
                st.session_state.selectedHarvestCycle = None
                st.session_state.farmManagementMessage = message
                st.rerun()
            else:
                st.warning(message)

    if st.session_state.selectedHarvestCycle is not None:
        selectedCycleID = int(
            st.session_state.selectedHarvestCycle
        )

        selectedCycleData = cycles[
            cycles["cycleID"] == selectedCycleID
        ]

        if selectedCycleData.empty:
            st.warning(
                "The selected crop cycle is unavailable."
            )

            st.session_state.selectedHarvestCycle = None

        else:
            selectedCycle = selectedCycleData.iloc[0]

            with st.expander(
                f"Record {selectedCycle['cropName']} harvest",
                expanded=True
            ):
                with st.form(
                    f"record_harvest_{selectedCycleID}"
                ):
                    actualQuantity = st.number_input(
                        "Actual quantity harvested",
                        min_value=0.01,
                        value=0.01
                    )

                    cycleUnit = str(
                        selectedCycle["expectedUnit"]
                    )

                    if cycleUnit in units:
                        unitIndex = units.index(cycleUnit)
                    else:
                        unitIndex = 0

                    actualUnit = st.selectbox(
                        "Unit",
                        units,
                        index=unitIndex
                    )

                    harvestDate = st.date_input(
                        "Harvest date",
                        value=date.today(),
                        max_value=date.today()
                    )
                    sellByDate = st.date_input(
                        "Sell-by date (optional)",
                        value=None
                    )
                    recordHarvestSubmitted = (
                        st.form_submit_button(
                            "Save harvested lot"
                        )
                    )
                    if (
                        sellByDate is not None
                        and sellByDate < harvestDate
                    ):
                        st.warning(
                            "The sell-by date cannot be before the harvest date."
                        )
                cancelHarvest = st.button(
                    "Cancel",
                    key="cancel_harvest"
                )

                if cancelHarvest:
                    st.session_state.selectedHarvestCycle = (
                        None
                    )

                    st.rerun()

                if recordHarvestSubmitted:
                    (
                        created,
                        lotID,
                        message
                    ) = addHarvestLot(
                        cycleID=selectedCycleID,
                        quantity=actualQuantity,
                        unit=actualUnit,
                        harvestDate=harvestDate,
                        sellByDate=sellByDate,
                        userID=userID
                    )

                    if created:
                        st.session_state.selectedHarvestCycle = None

                        st.session_state.farmManagementMessage = (
                            "Harvest recorded successfully. "
                            "You can upload images from Harvest management."
                        )

                        st.rerun()

                    else:
                        st.warning(message)

    # Update an existing crop-cycle forecast.
    with st.expander("Update a crop cycle"):
        cycleIDs = cycles[
            "cycleID"
        ].astype(int).tolist()

        def showCycle(cycleID):
            cycle = cycles[
                cycles["cycleID"] == cycleID
            ].iloc[0]

            return (
                f"{cycle['cropName']} — "
                f"{cycle['expectedYield']} "
                f"{cycle['expectedUnit']} — "
                f"{cycle['expectedHarvestDate']}"
            )

        selectedUpdateCycleID = st.selectbox(
            "Crop cycle",
            cycleIDs,
            format_func=showCycle,
            key="update_cycle"
        )

        updateCycle = cycles[
            cycles["cycleID"] == selectedUpdateCycleID
        ].iloc[0]

        currentUnit = str(updateCycle["expectedUnit"])

        if currentUnit not in units:
            units = units + [currentUnit]

        if currentUnit in units:
            updateUnitIndex = units.index(currentUnit)
        else:
            updateUnitIndex = 0

        selectedDate = updateCycle[
            "expectedHarvestDate"
        ]

        if hasattr(selectedDate, "date"):
            selectedDate = selectedDate.date()

        with st.form("update_cycle_form"):
            updatedExpectedYield = st.number_input(
                "Updated expected quantity",
                min_value=0.01,
                value=float(updateCycle["expectedYield"])
            )

            updatedExpectedUnit = st.selectbox(
                "Updated expected unit",
                units,
                index=updateUnitIndex
            )

            updatedExpectedDate = st.date_input(
                "Updated expected harvest date",
                value=selectedDate
            )

            updateCycleSubmitted = (
                st.form_submit_button(
                    "Save crop-cycle changes"
                )
            )

        if updateCycleSubmitted:
            updated = changeData(
                "UPDATE CropCycle SET expectedYield = %s, expectedUnit = %s, expectedHarvestDate = %s WHERE cycleID = %s AND farmID = %s",
                (
                    updatedExpectedYield,
                    updatedExpectedUnit,
                    updatedExpectedDate,
                    selectedUpdateCycleID,
                    farmID
                )
            )

            if updated:
                st.session_state.farmManagementMessage = (
                    "Crop cycle updated successfully."
                )

                st.rerun()


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

    farmOptions = makeOptions(farmData, "farmID", ["farmName", "location"])
    selectedFarmLabel = st.selectbox(
        "Farm",
        list(farmOptions.keys()),
        key="harvest_management_farm"
    )
    selectedFarmID = int(farmOptions[selectedFarmLabel])
    selectedFarm = farmData[farmData["farmID"] == selectedFarmID].iloc[0]
    farmName = str(selectedFarm["farmName"])
    farmLocation = str(selectedFarm["location"])

    st.subheader(farmName)
    st.caption(f"📍 {farmLocation}")

    # Get only crop cycles belonging to this farmer.
    cycles = selectData( """SELECT cc.cycleID, c.cropName, cc.expectedYield, cc.expectedUnit, cc.expectedHarvestDate
           FROM CropCycle cc, Crops c
           WHERE cc.cropID = c.cropID AND cc.farmID = %s AND cc.status = 1
             AND c.isActive = 1
           ORDER BY cc.expectedHarvestDate""", (selectedFarmID,))

    if cycles.empty:
        st.info("Add a crop cycle before recording a harvest.")

    else:
        cycleIDs = cycles["cycleID"].astype(int).tolist()

        def showCycle(cycleID):
            cycle = cycles[
                cycles["cycleID"] == cycleID
            ].iloc[0]

            return (
                f"{cycle['cropName']} — "
                f"expected {cycle['expectedYield']} "
                f"{cycle['expectedUnit']} on "
                f"{cycle['expectedHarvestDate']}"
            )

        with st.expander(
            "Record a harvested lot",
            expanded=True
        ):
            selectedCycleID = st.selectbox(
                "Crop cycle",
                cycleIDs,
                format_func=showCycle,
                key="harvest_cycle"
            )

            selectedCycle = cycles[
                cycles["cycleID"] == selectedCycleID
            ].iloc[0]

            quantityHarvested = st.number_input(
                "Quantity harvested",
                min_value=0.01,
                value=0.01,
                step=0.01,
                key="harvest_quantity"
            )

            expectedUnit = str(
                selectedCycle["expectedUnit"]
            )

            if expectedUnit in units:
                defaultUnitIndex = units.index(expectedUnit)
            else:
                defaultUnitIndex = 0

            unit = st.selectbox(
                "Unit",
                units,
                index=defaultUnitIndex,
                key="harvest_unit"
            )

            harvestDate = st.date_input(
                "Harvest date",
                value=date.today(),
                max_value=date.today(),
                key="harvest_date"
            )

            sellByDate = st.date_input(
                "Sell-by date (optional)",
                value=None,
                key="sell_by_date"
            )

            recordHarvestSubmitted = st.button(
                "Record harvest",
                key="record_harvest"
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
                        st.success(message)
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

    activeTab, historyTab = st.tabs(["Active inventory", "History"])

    with historyTab:
        if historyInventory.empty:
            st.info("There are no completed harvest lots for this farm.")
        else:
            historyDisplay = historyInventory[
                ["cropName", "quantityHarvested", "unit", "harvestDate",
                 "sellByDate", "stockOnHand", "reservedQuantity",
                 "availableQuantity", "saleStatus"]
            ].copy()
            historyDisplay.columns = [
                "Crop", "Quantity harvested", "Unit", "Harvest date",
                "Sell-by date", "Stock on hand", "Reserved", "Available",
                "Status"
            ]
            showTable(historyDisplay, use_container_width=True, hide_index=True)

    with activeTab:
        if activeInventory.empty:
            st.info("There are no active harvest lots for this farm.")
        else:
            activeDisplay = activeInventory[
                ["cropName", "quantityHarvested", "unit", "harvestDate",
                 "sellByDate", "stockOnHand", "reservedQuantity",
                 "availableQuantity", "saleStatus"]
            ].copy()
            activeDisplay.columns = [
                "Crop", "Quantity harvested", "Unit", "Harvest date",
                "Sell-by date", "Stock on hand", "Reserved", "Available",
                "Status"
            ]
            showTable(activeDisplay, use_container_width=True, hide_index=True)

    # The farmer chooses if a lot is active; stock level does not change this flag.
    with st.expander("Activate or deactivate a harvest lot"):
        statusOptions = {
            f"Lot {int(lot['lotID'])} - {lot['cropName']} ({'Active' if int(lot['status']) else 'Inactive'})": int(lot['lotID'])
            for _, lot in inventory.iterrows()
        }
        selectedStatusLabel = st.selectbox("Harvest lot", list(statusOptions))
        newStatus = st.radio("Set lot status", ["Active", "Inactive"], horizontal=True)
        if st.button("Save lot status"):
            changed, msg = setHarvestLotActive(statusOptions[selectedStatusLabel],
                                               newStatus == "Active", userID)
            if changed:
                st.success(msg)
                st.rerun()
            else:
                st.warning(msg)

    if activeInventory.empty:
        return

    inventory = activeInventory

    # Display inventory without database IDs.
    displayedInventory = inventory[
        [
            "cropName",
            "quantityHarvested",
            "unit",
            "harvestDate",
            "sellByDate",
            "stockOnHand",
            "reservedQuantity",
            "availableQuantity",
            "saleStatus"
        ]
    ].copy()

    displayedInventory.columns = [
        "Crop",
        "Quantity harvested",
        "Unit",
        "Harvest date",
        "Sell-by date",
        "Stock on hand",
        "Reserved",
        "Available",
        "Status"
    ]

    header1, header2, header3, header4 = st.columns(
        [2.5, 1.5, 1.5, 1]
    )

    header1.write("**Harvested produce**")
    header2.write("**Stock quantities**")
    header3.write("**Status**")
    header4.write("")

    for _, lot in inventory.iterrows():
        column1, column2, column3, column4 = st.columns(
            [2.5, 1.5, 1.5, 1]
        )

        with column1:
            st.write(f"**{lot['cropName']}**")

            st.caption(
                f"Harvested: {lot['harvestDate']}  \n"
                f"Sell by: {lot['sellByDate']}  \n"
                f"Original quantity: "
                f"{float(lot['quantityHarvested']):,.2f} "
                f"{lot['unit']}"
            )

        with column2:
            if pd.isna(lot["stockOnHand"]):
                st.write("Calculation unavailable")

            else:
                st.write(
                    f"On hand: "
                    f"{float(lot['stockOnHand']):,.2f} "
                    f"{lot['unit']}"
                )

                st.write(
                    f"Reserved: "
                    f"{float(lot['reservedQuantity']):,.2f} "
                    f"{lot['unit']}"
                )

                st.write(
                    f"Available: "
                    f"{float(lot['availableQuantity']):,.2f} "
                    f"{lot['unit']}"
                )

        with column3:
            st.write(str(lot["saleStatus"]))

            existingImages = selectData(
                """SELECT imageNumber
                FROM HarvestLotImages
                WHERE lotID = %s""",
                (int(lot["lotID"]),),
                show_error=False
            )

            st.caption(
                f"{len(existingImages)} image(s)"
            )

        with column4:
            if st.button(
                "Upload images",
                key=f"upload_images_{lot['lotID']}",
                use_container_width=True
            ):
                st.session_state.selectedImageLotID = int(
                    lot["lotID"]
                )

                st.rerun()

        st.divider()

  
    inventoryRows = inventory.index.tolist()

    def showLot(rowIndex):
        lot = inventory.loc[rowIndex]

        return (
            f"{lot['cropName']} — "
            f"harvested {lot['harvestDate']} — "
            f"{lot['availableQuantity']:,.2f} "
            f"{lot['unit']} available"
        )
    if st.session_state.selectedImageLotID is not None:
        selectedImageLotID = int(
            st.session_state.selectedImageLotID
        )

        selectedLotData = inventory[
            inventory["lotID"] == selectedImageLotID
        ]

        if selectedLotData.empty:
            st.warning(
                "The selected harvested lot is unavailable."
            )

            st.session_state.selectedImageLotID = None

        else:
            selectedImageLot = selectedLotData.iloc[0]

            with st.expander(
                f"Upload {selectedImageLot['cropName']} images",
                expanded=True
            ):
                uploadedFiles = st.file_uploader(
                    "Select JPG or PNG images",
                    type=["jpg", "jpeg", "png"],
                    accept_multiple_files=True,
                    key=f"inventory_images_{selectedImageLotID}"
                )

                uploadColumn, cancelColumn = st.columns(2)

                uploadSubmitted = uploadColumn.button(
                    "Upload selected images",
                    key=f"save_inventory_images_{selectedImageLotID}",
                    use_container_width=True
                )

                cancelUpload = cancelColumn.button(
                    "Cancel",
                    key=f"cancel_inventory_images_{selectedImageLotID}",
                    use_container_width=True
                )

                if cancelUpload:
                    st.session_state.selectedImageLotID = None
                    st.rerun()

                if uploadSubmitted:
                    if not uploadedFiles:
                        st.warning(
                            "Select at least one image."
                        )

                    else:
                        uploadedCount = 0
                        failedCount = 0

                        for uploadedFile in uploadedFiles:
                            uploaded, message = addHarvestImage(
                                selectedImageLotID,
                                uploadedFile,
                                userID
                            )

                            if uploaded:
                                uploadedCount += 1
                            else:
                                failedCount += 1

                        st.session_state.selectedImageLotID = None

                        if failedCount > 0:
                            st.session_state.imageUploadWarning = (
                                f"{uploadedCount} image(s) uploaded. "
                                f"{failedCount} image(s) could not "
                                f"be uploaded."
                            )

                        else:
                            st.session_state.imageUploadMessage = (
                                f"{uploadedCount} image(s) "
                                f"uploaded successfully."
                            )

                        st.rerun()
    adjustmentVersion = (
        st.session_state.stockAdjustmentVersion
    )
    with st.expander(
        "Record a loss or approved correction"
    ):
        selectedRow = st.selectbox(
            "Harvested stock",
            inventoryRows,
            format_func=showLot,
            key=f"adjustment_lot_{adjustmentVersion}"
        )

        movementType = st.selectbox(
            "Movement type",
            ["loss", "correction"],
            key=f"adjustment_type_{adjustmentVersion}"
        )
        selectedLot = inventory.loc[selectedRow]
        selectedLotID = int(selectedLot["lotID"])
        lotUnit = str(selectedLot["unit"])

        availableQuantity = float(
            selectedLot["availableQuantity"]
        )

        correctionConfirmed = True
        correctionEffect = None

        if movementType == "correction":
            correctionEffect = st.radio(
                "Correction effect",
                ["Add stock", "Remove stock"],
                horizontal=True,
                key=f"correction_effect_{adjustmentVersion}"
            )

            correctionConfirmed = st.checkbox(
                "I confirm that this correction has been approved",
                key=f"correction_confirmed_{adjustmentVersion}"
            )

        enteredQuantity = st.number_input(
            f"Quantity ({lotUnit})",
            min_value=0.01,
            value=0.01,
            step=0.01,
            key=f"adjustment_quantity_{adjustmentVersion}"
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
            key=f"adjustment_reason_{adjustmentVersion}"
        )

        submitted = st.button(
            "Record stock movement",
            key=f"record_adjustment_{adjustmentVersion}"
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

        showTable(
            displayedRequests,
            use_container_width=True,
            hide_index=True
        )

        requestRows = requests.index.tolist()

        def showRequest(rowIndex):
            request = requests.loc[rowIndex]

            return (
                f"{request['requestedBy']} — "
                f"{request['marketName']} — "
                f"{request['cropName']} — "
                f"{request['quantityRemaining']:,.2f} "
                f"{request['unit']} remaining"
            )

        selectedRequestRow = st.selectbox(
            "Request to allocate",
            requestRows,
            format_func=showRequest
        )

        selectedRequest = requests.loc[
            selectedRequestRow
        ]

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

        if lots.empty:
            st.warning(
                "Your farm has no compatible available "
                "lots for this request."
            )

        else:
            lotRows = lots.index.tolist()

            def showLot(rowIndex):
                lot = lots.loc[rowIndex]

                return (
                    f"{lot['cropName']} — "
                    f"{lot['availableQuantity']:,.2f} "
                    f"{lot['unit']} available — "
                    f"sell by {lot['sellByDate']}"
                )

            with st.form("allocation_form"):
                selectedLotRow = st.selectbox(
                    "Harvested stock",
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
                    f"Quantity to allocate ({requestUnit})",
                    min_value=0.01,
                    max_value=float(maximumAllocation),
                    value=min(
                        1.0,
                        float(maximumAllocation)
                    )
                )

                allocationSubmitted = (
                    st.form_submit_button(
                        "Reserve allocation"
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
    else:
        displayedAllocations = allocations.rename(
            columns={
                "allocationID": "Allocation #",
                "requestID": "Request #",
                "lotID": "Lot #",
                "requestedBy": "Requested by",
                "marketName": "Market",
                "cropName": "Crop",
                "quantityAllocated": "Quantity allocated",
                "unit": "Unit",
                "allocationDate": "Allocation date",
                "status": "Status"
            }
        )

        activeMask = allocations["status"].isin(
            ["reserved", "partiallyDelivered"]
        )
        activeAllocationTab, allocationHistoryTab = st.tabs(
            ["Active allocations", "Allocation history"]
        )

        with activeAllocationTab:
            activeDisplay = displayedAllocations[activeMask]
            if activeDisplay.empty:
                st.info("There are no active allocations.")
            else:
                showTable(activeDisplay, use_container_width=True, hide_index=True)

        with allocationHistoryTab:
            historyDisplay = displayedAllocations[~activeMask]
            if historyDisplay.empty:
                st.info("There are no past allocations.")
            else:
                showTable(historyDisplay, use_container_width=True, hide_index=True)

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

    displayedAllocations = allocations[
        [
            "requestedBy",
            "marketName",
            "cropName",
            "quantityAllocated",
            "unit",
            "reservedRemaining",
            "allocationDate",
            "status"
        ]
    ].copy()

    releasable = allocations[
        allocations["reservedRemaining"] > 0
    ]

    if releasable.empty:
        return

    releaseRows = releasable.index.tolist()

    def showReleasableAllocation(rowIndex):
        allocation = releasable.loc[rowIndex]

        return (
            f"{allocation['marketName']} — "
            f"{allocation['cropName']} — "
            f"{allocation['reservedRemaining']:,.2f} "
            f"{allocation['unit']} reserved"
        )

    with st.expander("Cancel reserved quantity"):
        with st.form(
            "cancel_reservation_form",
            clear_on_submit=True
):
            selectedAllocationRow = st.selectbox(
                "Allocation",
                releaseRows,
                format_func=showReleasableAllocation
            )

            selectedAllocation = releasable.loc[
                selectedAllocationRow
            ]

            maximumCancellation = float(
                selectedAllocation["reservedRemaining"]
            )

            cancellationQuantity = st.number_input(
                f"Quantity to cancel "
                f"({selectedAllocation['unit']})",
                min_value=0.01,
                max_value=maximumCancellation,
                value=min(
                    1.0,
                    maximumCancellation
                )
            )

            reason = st.text_input(
                "Reason (optional)"
            )

            cancelSubmitted = st.form_submit_button(
                "Cancel reserved quantity"
            )

        if cancelSubmitted:
            canceled, message = cancelReservedQuantity(
                allocationID=int(
                    selectedAllocation["allocationID"]
                ),
                cancellationQuantity=cancellationQuantity,
                reason=reason,
                userID=userID
            )

            if canceled:
                st.session_state.allocationMessage = (
                    f"Reservation canceled successfully. "
                    f"{cancellationQuantity:,.2f} "
                    f"{selectedAllocation['unit']} was "
                    f"returned to available stock."
                )

                st.rerun()

            else:
                st.warning(message)

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

    if markets.empty or crops.empty or not units:
        st.info(
            "Markets and crops must exist, and the application must have "
            "at least one supported unit configured."
        )

    else:
        marketOptions = makeOptions(
            markets,
            "marketID",
            ["marketName", "location"]
        )

        cropOptions = makeOptions(
            crops,
            "cropID",
            ["cropName"]
        )

        with st.expander(
            "Create a request",
            expanded=True
        ):
            with st.form(
                "new_request_form",
                clear_on_submit=True
            ):
                marketLabel = st.selectbox(
                    "Market",
                    list(marketOptions.keys())
                )

                cropLabel = st.selectbox(
                    "Crop",
                    list(cropOptions.keys())
                )

                quantity = st.number_input(
                    "Quantity requested",
                    min_value=0.01,
                    value=1.0
                )

                unit = st.selectbox(
                    "Unit",
                    units,
                    index=units.index("kg") if "kg" in units else 0
                )

                createSubmitted = (
                    st.form_submit_button(
                        "Create request"
                    )
                )

            if createSubmitted:
                created, message = createProduceRequest(
                    current_user=current_user,
                    cropID=int(cropOptions[cropLabel]),
                    marketID=int(marketOptions[marketLabel]),
                    quantity=quantity,
                    unit=unit
                )

                if created:
                    st.session_state.requestMessage = message
                    st.rerun()

                else:
                    st.warning(message)

    requests = selectData(
        """SELECT mr.requestID, m.marketName, m.location AS marketLocation, c.cropName, mr.quantityRequested, mr.unit, mr.requestDate, mr.reqStatus AS status
           FROM MarketRequests mr, Markets m, Crops c
           WHERE mr.marketID = m.marketID AND mr.cropID = c.cropID AND mr.userID = %s
           ORDER BY mr.requestDate DESC""",
        (userID,)
    )

    if requests.empty:
        st.info("You have not created any requests.")
        return

    requests["quantityRequested"] = pd.to_numeric(
        requests["quantityRequested"]
    )

    requests["quantityAllocated"] = 0.0
    requests["quantityReceived"] = 0.0
    requests["deliveryDifference"] = 0.0
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
            "deliveryDifference"
        ] = (
            progress["allocatedQuantity"]
            - progress["receivedQuantity"]
        )

        requests.at[
            index,
            "quantityRemaining"
        ] = progress["remainingQuantity"]

    st.subheader("Filter requests")

    filterColumn1, filterColumn2 = st.columns(2)
    filterColumn3, filterColumn4 = st.columns(2)

    marketNames = (
        ["All"] + sorted(
            requests["marketName"].unique().tolist()
        )
    )

    cropNames = (
        ["All"] + sorted(
            requests["cropName"].unique().tolist()
        )
    )

    selectedMarket = filterColumn1.selectbox(
        "Market",
        marketNames,
        key="request_filter_market"
    )

    selectedCrop = filterColumn2.selectbox(
        "Crop",
        cropNames,
        key="request_filter_crop"
    )

    selectedStatus = filterColumn3.selectbox(
        "Status",
        ["All"] + sorted(requestStatus),
        key="request_filter_status"
    )

    requestedAfter = filterColumn4.date_input(
        "Requested on or after",
        value=None,
        key="request_filter_date"
    )

    filteredRequests = requests.copy()

    if selectedMarket != "All":
        filteredRequests = filteredRequests[
            filteredRequests["marketName"] == selectedMarket
        ]

    if selectedCrop != "All":
        filteredRequests = filteredRequests[
            filteredRequests["cropName"] == selectedCrop
        ]

    if selectedStatus != "All":
        filteredRequests = filteredRequests[
            filteredRequests["status"] == selectedStatus
        ]

    if requestedAfter is not None:
        requestDates = pd.to_datetime(
            filteredRequests["requestDate"]
        ).dt.date

        filteredRequests = filteredRequests[
            requestDates >= requestedAfter
        ]

    st.subheader("Request records")

    if filteredRequests.empty:
        st.info(
            "No requests matched the selected filters."
        )

    else:
        displayedRequests = filteredRequests[
            [
                "marketName",
                "marketLocation",
                "cropName",
                "quantityRequested",
                "quantityAllocated",
                "quantityReceived",
                "deliveryDifference",
                "quantityRemaining",
                "unit",
                "requestDate",
                "status"
            ]
        ].copy()

        displayedRequests.columns = [
            "Market",
            "Market location",
            "Crop",
            "Requested",
            "Allocated",
            "Received",
            "Delivery difference",
            "Remaining request",
            "Unit",
            "Request date",
            "Status"
        ]

        showTable(
            displayedRequests,
            use_container_width=True,
            hide_index=True
        )

    editable = requests[
        requests["status"] == "pending"
    ]

    if editable.empty:
        return

    with st.expander(
        "Update or cancel a pending request"
    ):
        editableRows = editable.index.tolist()

        def showEditableRequest(rowIndex):
            request = editable.loc[rowIndex]

            return (
                f"{request['marketName']} — "
                f"{request['cropName']} — "
                f"{request['quantityRequested']:,.2f} "
                f"{request['unit']}"
            )

        selectedRequestRow = st.selectbox(
            "Request",
            editableRows,
            format_func=showEditableRequest
        )

        selectedRequest = editable.loc[
            selectedRequestRow
        ]

        currentQuantity = float(
            selectedRequest["quantityRequested"]
        )

        with st.form(
            "edit_request_form",
            clear_on_submit=True
        ):
            newQuantity = st.number_input(
                f"Requested quantity "
                f"({selectedRequest['unit']})",
                min_value=0.01,
                value=currentQuantity
            )

            action = st.radio(
                "Action",
                [
                    "Update quantity",
                    "Cancel request"
                ]
            )

            cancellationReason = st.text_input(
                "Cancellation reason "
                "(required only when canceling)"
            )

            editSubmitted = st.form_submit_button(
                "Save change"
            )

        if editSubmitted:
            changed, message = updateMarketRequest(
                requestID=int(
                    selectedRequest["requestID"]
                ),
                userID=userID,
                newQuantity=newQuantity,
                action=action,
                cancellationReason=cancellationReason
            )

            if changed:
                st.session_state.requestMessage = message
                st.rerun()

            else:
                st.warning(message)

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


def showDeliveries(current_user):
    st.header("Confirm deliveries")

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
                  a.allocStatus AS status
           FROM Allocations a, MarketRequests mr, Crops c
           WHERE a.requestID = mr.requestID AND mr.cropID = c.cropID
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

    if allocations.empty:
        st.info("There are no outstanding allocated deliveries to confirm.")
        receipts = selectData(
            """SELECT dr.receiptID, dr.requestID, dr.allocationID, c.cropName,
                      dr.expectedQuantity, dr.receivedQuantity,
                      dr.discrepancyQuantity, dr.unit, dr.deliveryDate
               FROM DeliveryReceipts dr, MarketRequests mr, Crops c
               WHERE dr.requestID = mr.requestID AND mr.cropID = c.cropID
               AND mr.userID = %s
               ORDER BY dr.deliveryDate DESC""",
            (userID,)
        )
        st.subheader("My delivery receipts")
        if receipts.empty:
            st.info("You have not confirmed any deliveries yet.")
        else:
            showTable(receipts, use_container_width=True, hide_index=True)
        return

    showTable(allocations, use_container_width=True, hide_index=True)
    allocationOptions = makeOptions(
        allocations,
        "allocationID",
        [
            "allocationID",
            "requestID",
            "cropName",
            "expectedNow",
            "unit",
        ]
    )

    with st.form("delivery_form"):
        allocationLabel = st.selectbox(
            "Allocation received",
            list(allocationOptions.keys())
        )
        selectedID = allocationOptions[allocationLabel]
        expectedNow = float(allocations.loc[
            allocations["allocationID"] == selectedID, "expectedNow"
        ].iloc[0])
        selectedUnit = str(allocations.loc[
            allocations["allocationID"] == selectedID, "unit"
        ].iloc[0])
        st.caption(f"Expected delivery: {expectedNow:,.2f} {selectedUnit}")
        receivedQuantity = st.number_input(
            "Quantity actually received",
            min_value=0.0,
            max_value=expectedNow,
            value=0.0
        )
        deliveryDate = st.date_input(
            "Delivery date",
            max_value=date.today()
        )
        submitted = st.form_submit_button("Confirm delivery")

        if submitted:
            confirmed, message = confirmDelivery(
                allocationOptions[allocationLabel],
                receivedQuantity,
                deliveryDate,
                userID
            )

            if confirmed:
                st.session_state.deliveryMessage = message
                st.rerun()
            else:
                st.warning(message)

    receipts = selectData(
        """SELECT dr.receiptID, dr.requestID, dr.allocationID, c.cropName,
                  dr.expectedQuantity, dr.receivedQuantity,
                  dr.discrepancyQuantity, dr.unit, dr.deliveryDate
           FROM DeliveryReceipts dr, MarketRequests mr, Crops c
           WHERE dr.requestID = mr.requestID AND mr.cropID = c.cropID
           AND mr.userID = %s
           ORDER BY dr.deliveryDate DESC""",
        (userID,)
    )

    st.subheader("My delivery receipts")
    if not receipts.empty:
        showTable(receipts, use_container_width=True, hide_index=True)


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

def showAdminUserManagement(current_user):
    st.header("User management and security audit")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator user management")
        return

    usersTab, auditTab = st.tabs(["User accounts", "Security activity"])

    with usersTab:
        users = selectData(
            """SELECT userID, fname, lname, role, email
               FROM Users
               ORDER BY fname, lname"""
        )

        if users.empty:
            st.info("There are no user accounts.")
        else:
            showTable(users, use_container_width=True, hide_index=True)
            userOptions = makeOptions(
                users,
                "userID",
                ["fname", "lname", "email", "role"]
            )

            with st.form("admin_change_role_form"):
                selectedUser = st.selectbox(
                    "User",
                    list(userOptions.keys())
                )
                newRole = st.selectbox("New role", sorted(roles))
                roleSubmitted = st.form_submit_button("Update role")

            if roleSubmitted:
                selectedUserID = int(userOptions[selectedUser])

                if selectedUserID == int(current_user["userID"]):
                    st.warning("You cannot change your own administrator role here.")
                elif changeData(
                    "UPDATE Users SET role = %s WHERE userID = %s",
                    (newRole, selectedUserID)
                ):
                    logActivity(
                        current_user["userID"],
                        "admin_role_changed",
                        f"Changed user {selectedUserID} to {newRole}"
                    )
                    st.success("User role updated successfully.")
                    st.rerun()

    with auditTab:
        if "permissionTestMessage" in st.session_state:
            st.success(st.session_state.permissionTestMessage)
            del st.session_state.permissionTestMessage

        with st.expander("Demonstrate denied farm access"):
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
            else:
                farmerOptions = makeOptions(
                    farmers,
                    "userID",
                    ["fname", "lname"]
                )
                farmOptions = makeOptions(
                    farms,
                    "farmID",
                    ["farmName", "location"]
                )

                selectedFarmer = st.selectbox(
                    "Farmer account",
                    list(farmerOptions.keys()),
                    key="permission_test_farmer"
                )
                selectedFarm = st.selectbox(
                    "Farm to test",
                    list(farmOptions.keys()),
                    key="permission_test_farm"
                )

                if st.button("Test farm permission"):
                    testedUserID = int(farmerOptions[selectedFarmer])
                    testedFarmID = int(farmOptions[selectedFarm])
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

        activity = selectData(
            """SELECT al.activityID,
                      CONCAT(u.fname, ' ', u.lname) AS performedBy,
                      u.role, al.actionType, al.actionDate, al.details
               FROM UserActivityLog al, Users u
               WHERE al.userID = u.userID
               ORDER BY al.actionDate DESC"""
        )

        if activity.empty:
            st.info("No security activity has been recorded yet.")
        else:
            actionOptions = ["All"] + sorted(
                activity["actionType"].unique().tolist()
            )
            selectedAction = st.selectbox("Activity type", actionOptions)

            if selectedAction != "All":
                activity = activity[
                    activity["actionType"] == selectedAction
                ]

            showTable(activity, use_container_width=True, hide_index=True)

def showAdminReferenceData(current_user):
    st.header("Administrator reference data")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator reference data")
        return

    user_tab, crop_tab, market_tab, unit_tab = st.tabs(
        ["Users", "Crops", "Markets", "Units"]
    )

    with user_tab:
        users = selectData(
            """SELECT userID, fname, lname, role, email
               FROM Users
               ORDER BY fname, lname"""
        )
        showTable(users, use_container_width=True, hide_index=True)

        with st.form("admin_add_user"):
            firstName = st.text_input("First name")
            lastName = st.text_input("Last name")
            role = st.selectbox("Role", sorted(roles))
            email = st.text_input("Email")
            password = st.text_input("Demonstration password", type="password")
            farm_name = ""
            farm_location = ""
            if role == "farmer":
                farm_name = st.text_input("Farm name")
                farm_location = st.text_input("Farm location or emirate")
            submitted = st.form_submit_button("Add user")

            if submitted:
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
                        st.success("User added successfully.")
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

    with crop_tab:
        st.subheader("Manage crops")
        st.caption("Inactive crops remain in past records but cannot be selected for new entries.")
        crops = selectData("SELECT cropID, cropName, isActive FROM Crops ORDER BY cropName")
        if not crops.empty:
            displayCrops = crops.copy()
            displayCrops["Status"] = displayCrops["isActive"].map(
                {1: "Active", 0: "Inactive", True: "Active", False: "Inactive"}
            )
            showTable(
                displayCrops[["cropID", "cropName", "Status"]].rename(
                    columns={"cropID": "Crop ID", "cropName": "Crop"}
                ), use_container_width=True, hide_index=True
            )

        if "adminCropMessage" in st.session_state:
            st.success(st.session_state.pop("adminCropMessage"))

        with st.form("admin_add_crop"):
            crop_name = st.text_input("New crop name")
            submitted = st.form_submit_button("Add crop")
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

        if not crops.empty:
            cropOptions = {
                f"{row['cropName']} (ID {int(row['cropID'])})": int(row['cropID'])
                for _, row in crops.iterrows()
            }
            chosen = st.selectbox("Existing crop", list(cropOptions), key="admin_crop_choice")
            selectedCrop = crops[crops["cropID"] == cropOptions[chosen]].iloc[0]
            with st.form(f"admin_edit_crop_{cropOptions[chosen]}"):
                updatedName = st.text_input("Edit crop name", value=str(selectedCrop["cropName"]))
                activeLabel = st.selectbox(
                    "Crop status", ["Active", "Inactive"],
                    index=0 if bool(selectedCrop["isActive"]) else 1
                )
                editSubmitted = st.form_submit_button("Save crop changes")
            if editSubmitted:
                success, message = changeReferenceCrop(
                    cropOptions[chosen], updatedName, activeLabel == "Active",
                    current_user["userID"]
                )
                if success:
                    st.session_state["adminCropMessage"] = message
                    st.rerun()
                else:
                    st.warning(message)

    with market_tab:
        st.subheader("Manage markets")
        st.caption("Inactive markets remain in earlier requests but cannot be chosen for new requests.")
        markets = selectData(
            "SELECT marketID, marketName, location, isActive FROM Markets ORDER BY marketName"
        )
        if not markets.empty:
            displayMarkets = markets.copy()
            displayMarkets["Status"] = displayMarkets["isActive"].map(
                {1: "Active", 0: "Inactive", True: "Active", False: "Inactive"}
            )
            showTable(
                displayMarkets[["marketID", "marketName", "location", "Status"]].rename(
                    columns={"marketID": "Market ID", "marketName": "Market", "location": "Location"}
                ), use_container_width=True, hide_index=True
            )

        if "adminMarketMessage" in st.session_state:
            st.success(st.session_state.pop("adminMarketMessage"))

        with st.form("admin_add_market"):
            market_name = st.text_input("New market name")
            market_location = st.text_input("New market location")
            submitted = st.form_submit_button("Add market")
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

        if not markets.empty:
            marketOptions = {
                f"{row['marketName']} (ID {int(row['marketID'])})": int(row['marketID'])
                for _, row in markets.iterrows()
            }
            chosen = st.selectbox("Existing market", list(marketOptions), key="admin_market_choice")
            selectedMarket = markets[markets["marketID"] == marketOptions[chosen]].iloc[0]
            with st.form(f"admin_edit_market_{marketOptions[chosen]}"):
                updatedName = st.text_input("Edit market name", value=str(selectedMarket["marketName"]))
                updatedLocation = st.text_input("Edit market location", value=str(selectedMarket["location"]))
                activeLabel = st.selectbox(
                    "Market status", ["Active", "Inactive"],
                    index=0 if bool(selectedMarket["isActive"]) else 1
                )
                editSubmitted = st.form_submit_button("Save market changes")
            if editSubmitted:
                success, message = changeReferenceMarket(
                    marketOptions[chosen], updatedName, updatedLocation,
                    activeLabel == "Active", current_user["userID"]
                )
                if success:
                    st.session_state["adminMarketMessage"] = message
                    st.rerun()
                else:
                    st.warning(message)

    with unit_tab:
        st.subheader("Manage units")
        st.caption("Inactive units stay in older records, but cannot be selected for new entries.")
        unitData = selectData(
            "SELECT unitName, isActive FROM Units ORDER BY unitName"
        )
        if not unitData.empty:
            displayUnits = unitData.copy()
            displayUnits["Status"] = displayUnits["isActive"].map(
                {1: "Active", 0: "Inactive", True: "Active", False: "Inactive"}
            )
            showTable(
                displayUnits[["unitName", "Status"]].rename(
                    columns={"unitName": "Unit"}
                ),
                use_container_width=True, hide_index=True
            )

        with st.form("admin_add_unit"):
            newUnit = st.text_input("New unit", placeholder="e.g. crate")
            addUnitSubmitted = st.form_submit_button("Add unit")
        if addUnitSubmitted:
            success, message = addReferenceUnit(newUnit, current_user["userID"])
            if success:
                st.session_state["adminUnitMessage"] = message
                st.rerun()
            else:
                st.warning(message)

        if "adminUnitMessage" in st.session_state:
            st.success(st.session_state.pop("adminUnitMessage"))

        if not unitData.empty:
            with st.form("admin_change_unit_status"):
                chosenUnit = st.selectbox(
                    "Existing unit", unitData["unitName"].astype(str).tolist()
                )
                currentlyActive = bool(
                    unitData.loc[unitData["unitName"] == chosenUnit, "isActive"].iloc[0]
                )
                desiredStatus = st.selectbox(
                    "Change to", ["Inactive", "Active"],
                    index=0 if currentlyActive else 1
                )
                statusSubmitted = st.form_submit_button("Save unit status")
            if statusSubmitted:
                success, message = setReferenceUnitActive(
                    chosenUnit, desiredStatus == "Active", current_user["userID"]
                )
                if success:
                    st.session_state["adminUnitMessage"] = message
                    st.rerun()
                else:
                    st.warning(message)

def showAdminOperations(current_user):
    st.header("Operational records")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator operational records")
        return

    table = st.selectbox(
        "Record type",
        [
            "Farms",
            "Farm ownership",
            "CropCycle",
            "HarvestLots",
            "MarketRequests",
            "Allocations",
            "StockMovements",
            "DeliveryReceipts",
            "Request status history",
            "Security activity",
        ],
    )

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

    records = selectData(allowed_queries[table])
    if records.empty:
        st.info("There are no records in this table.")
    else:
        showTable(records, use_container_width=True, hide_index=True)


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

    currentUser = st.session_state.currentUser
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
