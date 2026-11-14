import hashlib
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote_plus
from PIL import Image, UnidentifiedImageError
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from google import genai

load_dotenv(override=True)

st.set_page_config(page_title="Local Farm Market", page_icon="🌱", layout="wide")

IMAGE_FOLDER = Path(__file__).parent / "images"
IMAGE_FOLDER.mkdir(exist_ok=True)

roles = {"farmer", "user", "admin"}
requestStatus = {"pending", "partiallyFulfilled", "fullyFulfilled", "canceled"}
allocStatus = {"reserved", "partiallyDelivered", "delivered", "canceled"}
movementTypes = {"added", "reserved", "canceled", "delivered", "loss", "correction_added", "correction_removed"}
allowedUnits = ["g", "kg", "ton", "box", "piece"]


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


def hashPassword(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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
    return allowedUnits.copy()


def isValidUnit(unit):
    return str(unit).strip().lower() in allowedUnits


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

        enteredPasswordHash = hashPassword(password)

        if user[5] != enteredPasswordHash:
            return None, "The email or password is incorrect."

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


def showAuthentication():
    st.subheader("Account access")
    signInTab, signUpTab = st.tabs(["Sign in", "Sign up"])

    with signInTab:
        with st.form("sign_in_form"):
            signInEmail = st.text_input("Email", key="sign_in_email")
            signInPassword = st.text_input(
                "Password",
                type="password",
                key="sign_in_password"
            )
            signInSubmitted = st.form_submit_button("Sign in")

        if signInSubmitted:
            user, message = signInUser(signInEmail, signInPassword)

            if user:
                st.session_state.currentUser = user
                st.session_state.sessionStartedAt = datetime.now()
                st.success(message)
                st.rerun()
            else:
                st.error(message)

    with signUpTab:
        roleLabel = st.selectbox(
            "Account type",
            ["Farmer", "User"],
            key="sign_up_role"
        )

        with st.form("sign_up_form"):
            firstName = st.text_input("First name")
            lastName = st.text_input("Last name")
            signUpEmail = st.text_input("Email", key="sign_up_email")
            signUpPassword = st.text_input(
                "Password",
                type="password",
                key="sign_up_password"
            )
            confirmPassword = st.text_input(
                "Confirm password",
                type="password"
            )

            farmName = ""
            farmLocation = ""

            if roleLabel == "Farmer":
                farmName = st.text_input("First farm name")
                farmLocation = st.text_input("Farm location or emirate")

            signUpSubmitted = st.form_submit_button("Create account")

        if signUpSubmitted:
            if signUpPassword != confirmPassword:
                st.error("The passwords do not match.")
            else:
                role = "farmer" if roleLabel == "Farmer" else "user"
                created, message = signUpUser(
                    firstName,
                    lastName,
                    signUpEmail,
                    signUpPassword,
                    role,
                    farmName,
                    farmLocation
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
    stockOnHand, reservedQuantity, availableQuantity = (
        calculateLotAvailability(lotID)
    )

    if availableQuantity is None:
        return

    newStatus = 1 if float(availableQuantity) > 0 else 0
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE HarvestLots SET status = %s WHERE lotID = %s",
            (newStatus, lotID)
        )
        connection.commit()
    except Exception as error:
        if connection:
            connection.rollback()
        print("Harvest-lot status refresh error:", error)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def showDashboard(current_user):
    st.header("Availability dashboard")

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
    if available.empty:
        st.info("No harvested lots are available yet.")

    else:
        available["stockOnHand"] = 0.0
        available["reservedQuantity"] = 0.0
        available["availableQuantity"] = 0.0

        for index, lot in available.iterrows():
            lotID = int(lot["lotID"])

            stockOnHand, reservedQuantity, availableQuantity = (calculateLotAvailability(lotID))

            if availableQuantity is None:
                continue

            available.at[index, "stockOnHand"] = float(stockOnHand)

            available.at[index, "reservedQuantity"] = float( reservedQuantity)

            available.at[index, "availableQuantity"] = float(availableQuantity)

        totalsByUnit = (available.groupby("unit")[ [ "stockOnHand", "reservedQuantity", "availableQuantity"]] .sum().reset_index())

        col1, col2 = st.columns(2)

        col1.metric(
            "Harvest lots",
            len(available)
        )
        if expected.empty:
            activeCropCycles = 0
        else:
            activeCropCycles = (
                pd.to_datetime(expected["expectedHarvestDate"]).dt.date
                >= date.today()
            ).sum()

        col2.metric(
    "Active crop cycles",
    int(activeCropCycles)
)

        st.subheader("Quantity totals by unit")

        st.dataframe(
            totalsByUnit,
            use_container_width=True,
            hide_index=True
        )

        st.subheader("Harvested inventory")

        st.dataframe(
            available,
            use_container_width=True,
            hide_index=True
        )

    st.subheader("Expected harvest forecasts")

    if expected.empty:
        st.info("No expected harvests have been entered.")

    else:
        st.dataframe(
            expected,
            use_container_width=True,
            hide_index=True
        )

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

        minimumQuantity = col6.number_input(
            "Minimum available",
            min_value=0.0
        )

        produceQuery = """SELECT hl.lotID, cc.cropID, f.farmName, f.location, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
                        FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
                        WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND hl.status = 1 AND hl.harvestDate <= %s AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s)"""
        values = [
            date.today(),
            date.today()
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
                            imagePath = IMAGE_FOLDER / imageName

                            if imagePath.is_file():
                                st.image(
                                    str(imagePath),
                                    use_container_width=True
                                )
                            else:
                                st.caption("Image not available.")
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
            WHERE hl.lotID = %s AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND hl.status = 1""",(selectedLotID,) )

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
                    imagePath = IMAGE_FOLDER / str(image["imageName"])

                    if imagePath.is_file():
                        st.image(
                            str(imagePath),
                            caption=f"{lot['cropName']} — Lot {selectedLotID}",
                            use_container_width=True
                        )
                    else:
                        st.warning(
                            f"Image {image['imageNumber']} is not available."
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
def showImages(current_user):
    st.header("Harvest lot images")

    role = current_user["role"]
    user_id = int(current_user["userID"])

    # Farmers see only their own lots.
    if role == "farmer":
        lots = selectData("""SELECT hl.lotID, f.farmName,  c.cropName, hl.harvestDate
            FROM HarvestLots hl, CropCycle cc, Farms f, Crops c, OwnedBy o
            WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.farmID = o.farmID AND o.userID = %s
            ORDER BY hl.lotID DESC""",(user_id,))

    # Market users and administrators can view all lots.
    else:
        lots = selectData( """ SELECT hl.lotID,  f.farmName, c.cropName,  hl.harvestDate
            FROM HarvestLots hl,  CropCycle cc, Farms f,Crops c
            WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID
            ORDER BY  hl.lotID DESC""")

    if lots.empty:
        st.info("No harvest lots are available.")
        return

    options = makeOptions(
        lots,
        "lotID",
        [
            "lotID",
            "farmName",
            "cropName",
            "harvestDate"
        ]
    )

    selected_label = st.selectbox(
        "Select a harvest lot",
        list(options.keys()),
        key="selected_image_lot"
    )

    selected_lot_id = options[selected_label]

    # Only farmers can upload harvest images.
    if role == "farmer":
        with st.form("upload_image_form"):
            uploaded_file = st.file_uploader(
                "Upload JPG or PNG image",
                type=["jpg", "jpeg", "png"]
            )

            submitted = st.form_submit_button(
                "Upload image"
            )

        if submitted:
            if uploaded_file is None:
                st.warning("Choose an image first.")

            else:
                uploaded, message = addHarvestImage(
                    selected_lot_id,
                    uploaded_file,
                    user_id
                )

                if uploaded:
                    st.success(message)
                    st.rerun()
                else:
                    st.warning(message)

    images = selectData("""SELECT hi.imageNumber, hi.imageName, f.farmName,  c.cropName
        FROM HarvestLotImages hi,  HarvestLots hl,CropCycle cc, Farms f, Crops c
        WHERE hi.lotID = %s AND hi.lotID = hl.lotID AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID
        ORDER BY hi.imageNumber""", (selected_lot_id,))

    if images.empty:
        st.info("No images uploaded for this lot.")
        return

    for _, image_row in images.iterrows():
        image_number = image_row["imageNumber"]
        image_name = str(image_row["imageName"])

        caption = (
            f"Lot {selected_lot_id} - "
            f"{image_row['cropName']} - "
            f"{image_row['farmName']}"
        )

        image_path = IMAGE_FOLDER / image_name

        if image_path.is_file():
            try:
                st.image(
                    str(image_path),
                    caption=caption,
                    width=350
                )

            except Exception:
                st.error(
                    f"Image {image_number} is not available."
                )

        else:
            st.error(
                f"Image {image_number} is not available."
            )


def addHarvestImage(
    lotID,
    uploadedFile,
    userID
):
    connection = None
    cursor = None
    savedPath = None

    try:
        if uploadedFile is None:
            return False, "Choose an image first."

        if uploadedFile.size > 5 * 1024 * 1024:
            return False, "The image must be 5 MB or smaller."

        connection = getConnection()
        cursor = connection.cursor()

        # Confirm ownership and retrieve the crop name.
        cursor.execute("""SELECT c.cropName
               FROM HarvestLots hl, CropCycle cc, Farms f, Crops c, OwnedBy o
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.farmID = o.farmID AND hl.lotID = %s AND o.userID = %s""",(lotID, userID))

        cropResult = cursor.fetchone()

        if cropResult is None:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to upload an image to harvest lot {lotID}"
            )
            return False, (
                "This harvested lot does not belong to your farm."
            )

        cropName = str(cropResult[0]).strip().lower()

        safeCropName = re.sub(
            r"[^a-z0-9]+",
            "_",
            cropName
        ).strip("_")

        cursor.execute(
            """SELECT MAX(imageNumber)
               FROM HarvestLotImages
               WHERE lotID = %s""",
            (lotID,)
        )

        highestImageNumber = cursor.fetchone()[0]

        if highestImageNumber is None:
            imageNumber = 1
        else:
            imageNumber = highestImageNumber + 1

        fileName = (
            f"lot_{lotID}_{imageNumber}_"
            f"{safeCropName}.png"
        )

        savedPath = IMAGE_FOLDER / fileName

        try:
            uploadedFile.seek(0)
            image = Image.open(uploadedFile)
            image.verify()

            uploadedFile.seek(0)
            image = Image.open(uploadedFile).convert("RGB")

            image.save(
                savedPath,
                format="PNG"
            )

        except (UnidentifiedImageError, OSError):
            return False, (
                "Upload a valid JPG, JPEG, or PNG image."
            )

        cursor.execute(
            """INSERT INTO HarvestLotImages
               (imageNumber, lotID, imageName)
               VALUES (%s, %s, %s)""",
            (
                imageNumber,
                lotID,
                fileName
            )
        )

        connection.commit()
        logActivity(
            userID,
            "harvest_image_uploaded",
            f"Uploaded {fileName} for lot {lotID}"
        )

        return True, f"Image uploaded as {fileName}."

    except Exception as error:
        if connection:
            connection.rollback()

        if savedPath and savedPath.exists():
            savedPath.unlink()

        print("Image upload error:", error)

        return False, "The image could not be uploaded."

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
            st.dataframe(cycleHistory, use_container_width=True, hide_index=True)

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

        calculatedStatus = 1 if float(availableQuantity) > 0 else 0
        inventory.at[index, "status"] = calculatedStatus

        if int(lot["status"]) != calculatedStatus:
            refreshLotStatus(lotID)

        if sellByDateValue is not None and sellByDateValue < date.today():
            inventory.at[
                index,
                "saleStatus"
            ] = "past sell-by date"

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
            st.dataframe(historyDisplay, use_container_width=True, hide_index=True)

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
            st.dataframe(activeDisplay, use_container_width=True, hide_index=True)

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
                recorded = changeData(
                    """INSERT INTO StockMovements
                    (lotID, allocationID, movementType,
                        quantity, dateOccurred, reason, performedBy)
                    VALUES (%s, NULL, %s, %s, %s, %s, %s)""",
                    (
                        selectedLotID,
                        storedMovementType,
                        movementQuantity,
                        datetime.now(),
                        savedReason,
                        userID
                    )
                )


                if recorded:
                    refreshLotStatus(selectedLotID)
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
               AND cc.status = 1""",
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
                quantity, dateOccurred, reason, performedBy)
               VALUES (%s, NULL, %s, %s, %s, %s, %s)""",
            (
                lotID,
                "added",
                quantity,
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
               WHERE cc.farmID = o.farmIDAND cc.cycleID = %s AND o.userID = %s AND cc.status = 1""", (cycleID, userID)
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

        st.dataframe(
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
                st.dataframe(activeDisplay, use_container_width=True, hide_index=True)

        with allocationHistoryTab:
            historyDisplay = displayedAllocations[~activeMask]
            if historyDisplay.empty:
                st.info("There are no past allocations.")
            else:
                st.dataframe(historyDisplay, use_container_width=True, hide_index=True)

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

def createAllocation(
    requestID,
    lotID,
    quantity,
    userID
):
    connection = None
    cursor = None

    try:
        if quantity <= 0:
            return False, (
                "The allocation quantity must be greater than zero."
            )

        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT cropID, quantityRequested, unit, reqStatus
               FROM MarketRequests
               WHERE requestID = %s""",
            (requestID,))

        request = cursor.fetchone()

        if request is None:
            return False, (
                "The market request could not be found."
            )

        requestCropID = int(request[0])
        quantityRequested = float(request[1])
        requestUnit = str(request[2])
        requestStatus = str(request[3])

        if not isValidUnit(requestUnit):
            return False, "The request contains an unsupported unit."

        if requestStatus not in {
            "pending",
            "partiallyFulfilled"
        }:
            return False, (
                "This request is no longer open."
            )

        cursor.execute(
            """SELECT cc.cropID, hl.unit, hl.sellByDate
               FROM HarvestLots hl, CropCycle cc, OwnedBy o
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = o.farmID AND hl.lotID = %s AND o.userID = %s AND hl.status = 1""",
            (lotID, userID))

        lot = cursor.fetchone()

        if lot is None:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to allocate harvest lot {lotID}"
            )
            return False, (
                "This harvested lot does not belong "
                "to your farm."
            )

        lotCropID = int(lot[0])
        lotUnit = str(lot[1])
        sellByDate = None if lot[2] is None else pd.to_datetime(lot[2]).date()

        if not isValidUnit(lotUnit):
            return False, "The harvest lot contains an unsupported unit."

        if lotCropID != requestCropID:
            return False, (
                "The crop in this lot does not match "
                "the requested crop."
            )

        if lotUnit != requestUnit:
            return False, (
                "The lot unit does not match the request unit."
            )

        if sellByDate is not None and sellByDate < date.today():
            return False, (
                "This harvested lot has passed its sell-by date."
            )

        progress = calculateRequestProgress(requestID)

        if progress is None:
            return False, (
                "The request quantities could not be calculated."
            )

        remainingRequest = float(
            progress["remainingQuantity"]
        )

        if remainingRequest <= 0:
            return False, (
                "The full requested quantity has already "
                "been allocated."
            )

        if quantity > remainingRequest:
            return False, (
                f"Only {remainingRequest:,.2f} "
                f"{requestUnit} remains in this request."
            )

        (
            stockOnHand,
            reservedQuantity,
            availableQuantity
        ) = calculateLotAvailability(lotID)

        if availableQuantity is None:
            return False, (
                "The available quantity could not be calculated."
            )

        availableQuantity = float(
            availableQuantity
        )

        if quantity > availableQuantity:
            return False, (
                f"Only {availableQuantity:,.2f} "
                f"{lotUnit} is currently available."
            )

        allocationDate = datetime.now()

        cursor.execute(
            """INSERT INTO Allocations (requestID, lotID, allocatedBy, quantityAllocated, allocationDate, allocStatus)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                requestID,
                lotID,
                userID,
                quantity,
                allocationDate,
                "reserved"
            ))

        allocationID = cursor.lastrowid

        cursor.execute(
            """INSERT INTO StockMovements
            (lotID, allocationID, movementType,
                quantity, dateOccurred, reason, performedBy)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                lotID,
                allocationID,
                "reserved",
                quantity,
                allocationDate,
                f"Reserved for market request {requestID}",
                userID
            )
)

        updateRequestStatus(
            cursor=cursor,
            requestID=requestID,
            newStatus="partiallyFulfilled",
            changedBy=userID,
            reason="Produce reserved by farmer"
        )

        connection.commit()
        refreshLotStatus(lotID)
        logActivity(
            userID,
            "allocation_created",
            f"Created allocation {allocationID} for request {requestID}"
        )

        return (
            True,
            f"{quantity:,.2f} {lotUnit} was "
            f"reserved successfully."
        )

    except Exception as error:
        if connection:
            connection.rollback()

        print("Allocation error:", error)

        return False, (
            "The allocation could not be created."
        )

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()

def cancelReservedQuantity(
    allocationID,
    cancellationQuantity,
    reason,
    userID
):
    connection = None
    cursor = None

    try:
        if cancellationQuantity <= 0:
            return False, (
                "The cancellation quantity must be "
                "greater than zero."
            )

        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT a.requestID, a.lotID, a.quantityAllocated, hl.unit
               FROM Allocations a, HarvestLots hl, CropCycle cc, OwnedBy o
               WHERE a.lotID = hl.lotID AND hl.cycleID = cc.cycleID AND cc.farmID = o.farmID AND a.allocationID = %s AND o.userID = %s""",(allocationID, userID))

        allocation = cursor.fetchone()

        if allocation is None:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to cancel allocation {allocationID}"
            )
            return False, (
                "This allocation does not belong "
                "to your farm."
            )

        requestID = int(allocation[0])
        lotID = int(allocation[1])
        quantityAllocated = float(allocation[2])
        unit = str(allocation[3])

        cursor.execute(
            """SELECT movementType, quantity
               FROM StockMovements
               WHERE allocationID = %s
               ORDER BY dateOccurred""",(allocationID,))

        movements = cursor.fetchall()

        reservedRemaining = 0.0
        deliveredQuantity = 0.0

        for movementType, movementQuantity in movements:
            movementType = str(
                movementType
            ).strip().lower()

            movementQuantity = float(
                movementQuantity
            )

            if movementType == "reserved":
                reservedRemaining += movementQuantity

            elif movementType == "delivered":
                reservedRemaining -= movementQuantity
                deliveredQuantity += movementQuantity

            elif movementType == "canceled":
                reservedRemaining -= movementQuantity

        if reservedRemaining <= 0:
            return False, (
                "This allocation has no reserved quantity left."
            )

        if cancellationQuantity > reservedRemaining:
            return False, (
                f"Only {reservedRemaining:,.2f} "
                f"{unit} can be canceled."
            )

        if reason.strip():
            savedReason = reason.strip()
        else:
            savedReason = None

        cancellationDate = datetime.now()

        cursor.execute(
            """INSERT INTO StockMovements
            (lotID, allocationID, movementType,
                quantity, dateOccurred, reason, performedBy)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                lotID,
                allocationID,
                "canceled",
                cancellationQuantity,
                cancellationDate,
                savedReason,
                userID
            )
        )
        newAllocatedQuantity = (
            quantityAllocated - cancellationQuantity
        )

        if newAllocatedQuantity <= deliveredQuantity:
            if deliveredQuantity > 0:
                newAllocationStatus = "delivered"
            else:
                newAllocationStatus = "canceled"

        elif deliveredQuantity > 0:
            newAllocationStatus = "partiallyDelivered"

        else:
            newAllocationStatus = "reserved"

        cursor.execute(
            """UPDATE Allocations
               SET quantityAllocated = %s, allocStatus = %s
               WHERE allocationID = %s""",
            (
                newAllocatedQuantity,
                newAllocationStatus,
                allocationID
            ))

        # Check whether this request still has allocations.
        cursor.execute(
            """SELECT quantityAllocated, allocStatus
               FROM Allocations
               WHERE requestID = %s""",
            (requestID,))

        requestAllocations = cursor.fetchall()
        totalAllocated = 0.0

        for allocatedQuantity, allocationStatus in requestAllocations:
            allocationStatus = str(
                allocationStatus
            ).strip()

            if allocationStatus != "canceled":
                totalAllocated += float(
                    allocatedQuantity
                )

        if totalAllocated > 0:
            newRequestStatus = "partiallyFulfilled"
        else:
            newRequestStatus = "pending"

        updateRequestStatus(
            cursor=cursor,
            requestID=requestID,
            newStatus=newRequestStatus,
            changedBy=userID,
            reason=savedReason or "Reserved quantity released"
        )
        connection.commit()
        refreshLotStatus(lotID)
        logActivity(
            userID,
            "reservation_released",
            f"Released {cancellationQuantity} {unit} from allocation {allocationID}"
        )

        return (
            True,
            f"{cancellationQuantity:,.2f} "
            f"{unit} was released successfully."
        )

    except Exception as error:
        if connection:
            connection.rollback()

        print("Cancellation error:", error)

        return False, (
            "The reserved quantity could not be canceled."
        )

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
           ORDER BY marketName"""
    )

    crops = selectData(
        """SELECT cropID, cropName
           FROM Crops
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

        st.dataframe(
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
               WHERE requestID = %s AND userID = %s""",
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
            st.dataframe(receipts, use_container_width=True, hide_index=True)
        return

    st.dataframe(allocations, use_container_width=True, hide_index=True)
    allocationOptions = makeOptions(
        allocations,
        "allocationID",
        [
            "allocationID",
            "requestID",
            "cropName",
            "quantityAllocated",
            "unit",
        ]
    )

    with st.form("delivery_form"):
        allocationLabel = st.selectbox(
            "Allocation received",
            list(allocationOptions.keys())
        )
        receivedQuantity = st.number_input(
            "Quantity actually received",
            min_value=0.0,
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
        st.dataframe(receipts, use_container_width=True, hide_index=True)


def confirmDelivery(allocationID, receivedQuantity, deliveryDate, userID):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """SELECT a.requestID, a.lotID, a.quantityAllocated,
                      mr.userID, mr.quantityRequested, mr.unit,
                      a.allocStatus
               FROM Allocations a, MarketRequests mr
               WHERE a.requestID = mr.requestID
               AND a.allocationID = %s""",
            (allocationID,)
        )
        allocation = cursor.fetchone()

        if allocation is None:
            return False, "The allocation could not be found."

        if int(allocation[3]) != userID:
            logActivity(
                userID,
                "access_denied",
                f"Attempted to confirm allocation {allocationID}"
            )
            return False, "You cannot confirm another user's delivery."

        requestID = int(allocation[0])
        lotID = int(allocation[1])
        expectedQuantity = float(allocation[2])
        requestedQuantity = float(allocation[4])
        unit = str(allocation[5])
        allocationStatus = str(allocation[6])

        if not isValidUnit(unit):
            return False, "The allocation contains an unsupported unit."

        if allocationStatus not in {"reserved", "partiallyDelivered"}:
            return False, "This allocation is no longer awaiting confirmation."

        cursor.execute(
            "SELECT receiptID FROM DeliveryReceipts WHERE allocationID = %s",
            (allocationID,)
        )

        if cursor.fetchone() is not None:
            return False, "A receipt has already been recorded for this allocation."

        receivedQuantity = float(receivedQuantity)

        if receivedQuantity < 0:
            return False, "The received quantity cannot be negative."

        discrepancyQuantity = expectedQuantity - receivedQuantity
        movementDate = datetime.combine(deliveryDate, datetime.min.time())

        if receivedQuantity > expectedQuantity:
            extraQuantity = receivedQuantity - expectedQuantity
            _, _, availableQuantity = calculateLotAvailability(lotID)

            if availableQuantity is None or extraQuantity > float(availableQuantity):
                return False, "The extra received quantity exceeds available stock."

            cursor.execute(
                """INSERT INTO StockMovements
                   (lotID, allocationID, movementType, quantity,
                    dateOccurred, reason, performedBy)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    lotID,
                    allocationID,
                    "reserved",
                    extraQuantity,
                    movementDate,
                    "Extra quantity confirmed during delivery",
                    userID
                )
            )

        cursor.execute(
            """INSERT INTO DeliveryReceipts
               (requestID, allocationID, expectedQuantity,
                receivedQuantity, discrepancyQuantity, unit,
                deliveryDate, confirmedBy)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                requestID,
                allocationID,
                expectedQuantity,
                receivedQuantity,
                discrepancyQuantity,
                unit,
                movementDate,
                userID
            )
        )

        if receivedQuantity > 0:
            cursor.execute(
                """INSERT INTO StockMovements
                   (lotID, allocationID, movementType, quantity,
                    dateOccurred, reason, performedBy)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    lotID,
                    allocationID,
                    "delivered",
                    receivedQuantity,
                    movementDate,
                    "Delivery confirmed by market buyer",
                    userID
                )
            )

        if discrepancyQuantity > 0:
            cursor.execute(
                """INSERT INTO StockMovements
                   (lotID, allocationID, movementType, quantity,
                    dateOccurred, reason, performedBy)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    lotID,
                    allocationID,
                    "canceled",
                    discrepancyQuantity,
                    movementDate,
                    "Undelivered quantity released",
                    userID
                )
            )

        cursor.execute(
            "UPDATE Allocations SET allocStatus = %s WHERE allocationID = %s",
            ("delivered", allocationID)
        )

        cursor.execute(
            """SELECT dr.receivedQuantity
               FROM DeliveryReceipts dr
               WHERE dr.requestID = %s""",
            (requestID,)
        )
        totalReceived = sum(float(row[0]) for row in cursor.fetchall())
        newRequestStatus = (
            "fullyFulfilled"
            if totalReceived >= requestedQuantity
            else "partiallyFulfilled"
        )
        updateRequestStatus(
            cursor,
            requestID,
            newRequestStatus,
            userID,
            "Delivery receipt confirmed"
        )

        connection.commit()
        refreshLotStatus(lotID)
        logActivity(
            userID,
            "delivery_confirmed",
            f"Confirmed allocation {allocationID}; discrepancy {discrepancyQuantity} {unit}"
        )

        message = (
            f"Delivery confirmed. Discrepancy: "
            f"{discrepancyQuantity:,.2f} {unit}."
        )
        return True, message

    except Exception as error:
        if connection:
            connection.rollback()
        print("Delivery confirmation error:", error)
        return False, "The delivery could not be confirmed. Please check the entered values."
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
            st.dataframe(users, use_container_width=True, hide_index=True)
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

            st.dataframe(activity, use_container_width=True, hide_index=True)

def showAdminReferenceData(current_user):
    st.header("Administrator reference data")

    if current_user["role"] != "admin":
        denyAccess(current_user, "Attempted to open administrator reference data")
        return

    user_tab, crop_tab, market_tab = st.tabs(
        ["Users", "Crops", "Markets"]
    )

    with user_tab:
        users = selectData(
            """SELECT userID, fname, lname, role, email
               FROM Users
               ORDER BY fname, lname"""
        )
        st.dataframe(users, use_container_width=True, hide_index=True)

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
        crops = selectData("SELECT cropID, cropName FROM Crops ORDER BY cropName")
        st.dataframe(crops, use_container_width=True, hide_index=True)

        with st.form("admin_add_crop"):
            crop_name = st.text_input("Crop name")
            submitted = st.form_submit_button("Add crop")

            if submitted:
                if not crop_name.strip():
                    st.warning("Enter a crop name.")
                elif changeData(
                    "INSERT INTO Crops (cropName) VALUES (%s)",
                    (crop_name.strip(),),
                ):
                    st.success("Crop added successfully.")
                    st.rerun()

    with market_tab:
        markets = selectData(
            "SELECT marketID, marketName, location FROM Markets ORDER BY marketName"
        )
        st.dataframe(markets, use_container_width=True, hide_index=True)

        with st.form("admin_add_market"):
            market_name = st.text_input("Market name")
            market_location = st.text_input("Market location")
            submitted = st.form_submit_button("Add market")

            if submitted:
                if not market_name.strip() or not market_location.strip():
                    st.warning("Enter the market name and location.")
                else:
                    query = "INSERT INTO Markets (marketName, location) VALUES (%s, %s)"
                    if changeData(query, (market_name.strip(), market_location.strip())):
                        st.success("Market added successfully.")
                        st.rerun()

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
        st.dataframe(records, use_container_width=True, hide_index=True)


# -----------------------------------------------------------------------------
# Read-only LangChain assistant
# -----------------------------------------------------------------------------
def getGeminiClient():
    apiKey = os.getenv("GEMINI_API_KEY")

    if not apiKey:
        raise ValueError(
            "The database assistant is not configured."
        )

    return genai.Client(api_key=apiKey)


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


def isSafeSelectQuery(sql):
    sqlClean = sql.strip().lower()

    if not sqlClean.startswith("select"):
        return False

    # Only permit one SQL statement.
    if ";" in sqlClean.rstrip(";"):
        return False

    blockedWords = [
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "create",
        "replace",
        "rename",
        "grant",
        "revoke",
        "outfile",
        "dumpfile",
        "load_file",
        "sleep",
        "benchmark",
        "passwordhash"
    ]

    for word in blockedWords:
        if re.search(rf"\b{word}\b", sqlClean):
            return False

    blockedDatabases = [
        "information_schema",
        "mysql.",
        "performance_schema",
        "sys."
    ]

    for databaseName in blockedDatabases:
        if databaseName in sqlClean:
            return False

    return True


def generateSQL(question, schemaText, currentUser):
    client = getGeminiClient()

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
produce to them.
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

    response = client.models.generate_content(
        model=getModelName(),
        contents=prompt
    )

    return extractSQL(response.text)


def runAssistantSQL(sql):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()
        cursor.execute(sql)

        rows = cursor.fetchall()
        columns = [
            column[0]
            for column in cursor.description
        ]

        return pd.DataFrame(
            rows,
            columns=columns
        )

    except Exception as error:
        print("Assistant query error:", error)
        return pd.DataFrame()

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


def summarizeAssistantAnswer(question, sql, results):
    client = getGeminiClient()

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

    response = client.models.generate_content(
        model=getModelName(),
        contents=prompt
    )

    return response.text.strip()

def showAssistant(current_user):
    st.header("Database assistant")

    st.caption(
        "Ask a natural-language question about database records. "
        "Only validated read-only SELECT queries can be executed."
    )

    exampleQuestions = [
        "Which crops are currently available?",
        "Which harvest lots have no available stock?",
        "What are the active expected harvests?",
        "Show pending market requests",
        "Show delivery discrepancies"
    ]

    selectedExample = st.selectbox(
        "Example questions",
        ["Write my own question"] + exampleQuestions
    )

    if selectedExample == "Write my own question":
        question = st.text_input(
            "Your question",
            placeholder="For example: Which crops are currently available?"
        )
    else:
        question = selectedExample

    askSubmitted = st.button(
        "Ask assistant",
        use_container_width=True
    )

    if not askSubmitted:
        return

    if not question.strip():
        st.warning("Enter a question first.")
        return

    try:
        schemaText = getSchemaText()

        if not schemaText:
            st.warning(
                "The database structure could not be read."
            )
            return

        with st.spinner("Preparing your answer..."):
            sql = generateSQL(
                question,
                schemaText,
                current_user
            )

            if sql == "NOT_ANSWERABLE":
                st.info(
                    "This question cannot be answered clearly "
                    "using the current database."
                )
                return

            if not isSafeSelectQuery(sql):
                st.warning(
                    "The generated query was blocked because "
                    "it was not a safe read-only query."
                )
                return

            results = runAssistantSQL(sql)

            answer = summarizeAssistantAnswer(
                question,
                sql,
                results
            )

        st.subheader("Answer")
        st.write(answer)

        with st.expander("Supporting records"):
            if results.empty:
                st.info("No matching records were found.")
            else:
                st.dataframe(
                    results,
                    use_container_width=True,
                    hide_index=True
                )

        with st.expander("Generated read-only SQL"):
            st.code(
                sql,
                language="sql"
            )

    except ValueError:
        st.warning(
            "The database assistant is not configured. "
            "Check GEMINI_API_KEY in the .env file."
        )

    except SQLAlchemyError as error:
        print("Assistant database error:", error)
        st.error(
            "The database assistant could not retrieve the records."
        )

    except Exception as error:
        print("Assistant error:", error)
        st.error(
            "The assistant could not answer this question right now."
        )


# -----------------------------------------------------------------------------
# Application setup and navigation
# -----------------------------------------------------------------------------


def main():
    st.title("Local Farm and Market Food Availability System")

    st.sidebar.header("Database connection")
    st.sidebar.caption(
        f"Using {os.getenv('DB_NAME', 'DBProjGroup3')} from the .env file"
    )

    if st.sidebar.button("Test database connection"):
        test = selectData("SELECT 1 AS connected")

        if not test.empty:
            st.sidebar.success(
                f"Connected to {os.getenv('DB_NAME', 'DBProjGroup3')}."
            )

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

    st.sidebar.write(
        f"Signed in as **{currentUser['fullname']}**"
    )
    st.sidebar.success(f"Role: {role}")

    if st.sidebar.button("Sign out"):
        sessionStarted = st.session_state.get("sessionStartedAt")
        details = "User signed out"

        if sessionStarted:
            duration = datetime.now() - sessionStarted
            details = f"User signed out after {str(duration).split('.')[0]}"

        logActivity(currentUser["userID"], "logout", details)
        st.session_state.currentUser = None
        st.session_state.pop("sessionStartedAt", None)
        st.session_state.pop("selectedProduceLot", None)
        st.rerun()

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

    # Reject an invalid or unknown role
    if role not in rolePages:
        st.error("Your account does not have a valid role.")
        return

    pages = rolePages[role]
    page = st.sidebar.radio("Navigation", pages)

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


if __name__ == "__main__":
    main()
