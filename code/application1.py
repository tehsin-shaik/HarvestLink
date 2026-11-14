import hashlib
import os
import re
from datetime import date, datetime
from pathlib import Path
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
movementTypes = {"added", "reserved", "canceled", "delivered", "loss", "correctionAdd", "correctionRemove"}


# -----------------------------------------------------------------------------
# Database helpers
# -----------------------------------------------------------------------------

def buildDbUri() -> str:
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "Stella24_!")  # Change to your password
    db_name = os.getenv("DB_NAME", "DBProjGroup3")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db_name}"


def getGeminiClient():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in .env")
    return genai.Client(api_key=api_key)


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
    if error_number == 1045:
        return "The database username or password is incorrect."
    if error_number in (2003, 2005):
        return "Could not connect to MySQL. Check that MySQL is running."

    return "Please check the entered values."


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


def isValidName(username):
    return re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,49}", username) is not None


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

        return signedInUser, "Signed in successfully."

    except Exception as error:
        print("LOGIN ERROR:", error)
        return None, friendlyDatabaseError(error)

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()

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
        """SELECT status
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
           SET status = %s
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
           AND NOT EXISTS (
               SELECT 1
               FROM HarvestLots hl
               WHERE hl.cycleID = cc.cycleID
           )
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
            available.at[index, "stockOnHand"] = float(stockOnHand)

            available.at[index, "reservedQuantity"] = float( reservedQuantity)

            available.at[index, "availableQuantity"] = float(availableQuantity)

        totalsByUnit = (available.groupby("unit")[ [ "stockOnHand", "reservedQuantity", "availableQuantity"]] .sum().reset_index())

        col1, col2 = st.columns(2)

        col1.metric(
            "Harvest lots",
            len(available)
        )
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
            return False, (
                "Only market buyers can create requests."
            )

        if quantity <= 0:
            return False, (
                "The requested quantity must be greater than zero."
            )

        connection = getConnection()
        cursor = connection.cursor()

        requestDate = datetime.now()

        cursor.execute(
            """INSERT INTO MarketRequests
               (userID, marketID, cropID,
                quantityRequested, unit,
                requestDate, status)
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
        st.warning("This page is available only to market buyers.")
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

    # Show the filters only while viewing the listing page.
    if st.session_state.selectedProduceLot is None:
        col1, col2, col3 = st.columns(3)
        col4, col5, col6 = st.columns(3)

        selectedCrop = col1.selectbox("Crop", cropNames)
        selectedFarm = col2.selectbox("Farm", farmNames)
        location = col3.text_input("Location or emirate")

        selectedUnit = col4.selectbox(
            "Unit",
            ["All", "g", "kg", "ton", "piece", "box"]
        )

        minimumQuantity = col6.number_input(
            "Minimum available",
            min_value=0.0
        )

        produceQuery = """SELECT hl.lotID, cc.cropID, f.farmName, f.location, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
                        FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
                        WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND hl.harvestDate <= %s AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s)"""
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
            WHERE hl.lotID = %s AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID""",(selectedLotID,) )

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

                if requestedQuantity > float(latestAvailability):
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
            FROM HarvestLots hl,  CropCycle cc, Farms f, Crops c
            WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID  AND cc.cropID = c.cropID AND f.ownerID = %s
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
                addHarvestImage(
                    selected_lot_id,
                    uploaded_file,
                    user_id
                )

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
               FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND hl.lotID = %s AND f.ownerID = %s""",(lotID, userID))

        cropResult = cursor.fetchone()

        if cropResult is None:
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
        st.warning("This page is available only to farmers.")
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
        """SELECT farmID, farmName, location
           FROM Farms
           WHERE ownerID = %s
           LIMIT 1""",
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
                created = changeData(
                    """INSERT INTO Farms
                       (ownerID, farmName, location)
                       VALUES (%s, %s, %s)""",
                    (
                        userID,
                        farmName,
                        farmLocation
                    )
                )

                if created:
                    st.session_state.farmManagementMessage = (
                        "Farm created successfully."
                    )

                    st.rerun()

        return

    farmID = int(farmData.iloc[0]["farmID"])
    farmName = str(farmData.iloc[0]["farmName"])
    farmLocation = str(farmData.iloc[0]["location"])

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
                       AND ownerID = %s""",
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
                ["g", "kg", "ton", "box", "piece"],
                index=1
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
            created = changeData("INSERT INTO CropCycle(farmID, cropID, expectedUnit, expectedYield, expectedHarvestDate) VALUES (%s, %s, %s, %s, %s)",
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
       WHERE cc.cropID = c.cropID AND cc.farmID = %s AND NOT EXISTS ( SELECT 1 FROM HarvestLots hl WHERE hl.cycleID = cc.cycleID)
       ORDER BY cc.expectedHarvestDate""",(farmID,))

    st.subheader("Active crop cycles")
    if cycles.empty:
        st.info("No crop cycles have been entered.")
        return

    # Display cycle rows with a Record harvest button.
    heading1, heading2, heading3, heading4 = st.columns(
        [2, 2, 2, 1]
    )

    heading1.write("**Crop**")
    heading2.write("**Expected quantity**")
    heading3.write("**Expected harvest date**")
    heading4.write("")

    for _, cycle in cycles.iterrows():
        column1, column2, column3, column4 = (
            st.columns([2, 2, 2, 1])
        )

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
            key=f"record_cycle_{cycle['cycleID']}"
        ):
            st.session_state.selectedHarvestCycle = int(
                cycle["cycleID"]
            )

            st.session_state.newHarvestLotID = None
            st.rerun()

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

                    units = [
                        "g",
                        "kg",
                        "ton",
                        "box",
                        "piece"
                    ]

                    cycleUnit = str(
                        selectedCycle["expectedUnit"]
                    )

                    if cycleUnit in units:
                        unitIndex = units.index(cycleUnit)
                    else:
                        unitIndex = 1

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

        units = ["g", "kg", "ton", "box", "piece"]
        currentUnit = str(updateCycle["expectedUnit"])

        if currentUnit in units:
            updateUnitIndex = units.index(currentUnit)
        else:
            updateUnitIndex = 1

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
        st.warning("This page is available only to farmers.")
        return
    
    userID = int(current_user["userID"])

    # Get the farmer's farm.
    farmData = selectData(
        """SELECT farmID, farmName, location
           FROM Farms
           WHERE ownerID = %s
           LIMIT 1""",
        (userID,)
    )

    if farmData.empty:
        st.info("Create your farm before recording harvests.")
        return

    farmName = str(farmData.iloc[0]["farmName"])
    farmLocation = str(farmData.iloc[0]["location"])

    st.subheader(farmName)
    st.caption(f"📍 {farmLocation}")

    # Get only crop cycles belonging to this farmer.
    cycles = selectData( """SELECT cc.cycleID, c.cropName, cc.expectedYield, cc.expectedUnit,  cc.expectedHarvestDate
           FROM CropCycle cc, Farms f, Crops c
           WHERE cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.ownerID = %s
           ORDER BY cc.expectedHarvestDate""", (userID,))

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

            units = ["g", "kg", "ton", "box", "piece"]
            expectedUnit = str(
                selectedCycle["expectedUnit"]
            )

            if expectedUnit in units:
                defaultUnitIndex = units.index(expectedUnit)
            else:
                defaultUnitIndex = 1

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
                "Sell-by date",
                value=date.today(),
                min_value=harvestDate,
                key="sell_by_date"
            )

            recordHarvestSubmitted = st.button(
                "Record harvest",
                key="record_harvest"
            )

            if recordHarvestSubmitted:
                if sellByDate < harvestDate:
                    st.warning(
                        "The sell-by date cannot be before "
                        "the harvest date."
                    )

                else:
                    created, message = addHarvestLot(
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


    inventory = selectData( """SELECT hl.lotID, c.cropName, hl.quantityHarvested, hl.unit, hl.harvestDate, hl.sellByDate
           FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
           WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.ownerID = %s ORDER BY hl.harvestDate DESC, c.cropName""",(userID,))

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

        sellByDateValue = pd.to_datetime(
            lot["sellByDate"]
        ).date()

        if sellByDateValue < date.today():
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
               FROM CropCycle cc, Farms f
               WHERE cc.farmID = f.farmID
               AND cc.cycleID = %s
               AND f.ownerID = %s""",
            (cycleID, userID)
        )

        ownedCycle = cursor.fetchone()

        if ownedCycle is None:
            return False, None, (
                "This crop cycle does not belong to your farm."
            )

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

        lotID = cursor.lastrowid

        movementDate = datetime.combine(
            harvestDate,
            datetime.min.time()
        )

        cursor.execute(
            """INSERT INTO StockMovements
               (lotID, allocationID, movementType,
                quantity, dateOccurred, reason)
               VALUES (%s, NULL, %s, %s, %s, %s)""",
            (
                lotID,
                "added",
                quantity,
                movementDate,
                "Initial harvested quantity"
            )
        )

        connection.commit()

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


def showFarmerAllocations(current_user):
    st.header("Market requests and allocations")

    if current_user["role"] != "farmer":
        st.warning("This page is available only to farmers.")
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
        requests["quantityRemaining"] = 0.0

        for index, request in requests.iterrows():
            requestID = int(request["requestID"])

            existingAllocations = selectData(
                """SELECT quantityAllocated, status
                   FROM Allocations
                   WHERE requestID = %s""",
                (requestID,))

            allocatedQuantity = 0.0

            if not existingAllocations.empty:
                for _, allocation in existingAllocations.iterrows():
                    allocationStatus = str(
                        allocation["status"]
                    ).strip()

                    if allocationStatus != "canceled":
                        allocatedQuantity += float(
                            allocation["quantityAllocated"]
                        )

            requestedQuantity = float(
                request["quantityRequested"]
            )

            requests.at[
                index,
                "quantityAllocated"
            ] = allocatedQuantity

            requests.at[
                index,
                "quantityRemaining"
            ] = requestedQuantity - allocatedQuantity

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
            "Allocated",
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
               FROM HarvestLots hl, CropCycle cc, Farms f, Crops c
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND cc.cropID = c.cropID AND f.ownerID = %s AND c.cropID = %s AND hl.unit = %s AND hl.harvestDate <= %s AND (hl.sellByDate IS NULL OR hl.sellByDate >= %s)
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

        st.dataframe(
            displayedAllocations,
            use_container_width=True,
            hide_index=True
        )

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
            """SELECT cropID, quantityRequested, unit, status
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

        if requestStatus not in {
            "pending",
            "partiallyFulfilled"
        }:
            return False, (
                "This request is no longer open."
            )

        cursor.execute(
            """SELECT cc.cropID, hl.unit, hl.sellByDate
               FROM HarvestLots hl, CropCycle cc, Farms f
               WHERE hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND hl.lotID = %s AND f.ownerID = %s""",
            (lotID, userID))

        lot = cursor.fetchone()

        if lot is None:
            return False, (
                "This harvested lot does not belong "
                "to your farm."
            )

        lotCropID = int(lot[0])
        lotUnit = str(lot[1])
        sellByDate = pd.to_datetime(lot[2]).date()

        if lotCropID != requestCropID:
            return False, (
                "The crop in this lot does not match "
                "the requested crop."
            )

        if lotUnit != requestUnit:
            return False, (
                "The lot unit does not match the request unit."
            )

        if sellByDate < date.today():
            return False, (
                "This harvested lot has passed its sell-by date."
            )

        cursor.execute(
            """SELECT quantityAllocated, status
               FROM Allocations
               WHERE requestID = %s""",
            (requestID,))

        existingAllocations = cursor.fetchall()
        alreadyAllocated = 0.0

        for allocatedQuantity, allocationStatus in existingAllocations:
            allocationStatus = str(
                allocationStatus
            ).strip()

            if allocationStatus != "canceled":
                alreadyAllocated += float(
                    allocatedQuantity
                )

        remainingRequest = (
            quantityRequested - alreadyAllocated
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
            """INSERT INTO Allocations (requestID, lotID, quantityAllocated, allocationDate, status)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                requestID,
                lotID,
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
               FROM Allocations a, HarvestLots hl, CropCycle cc, Farms f
               WHERE a.lotID = hl.lotID AND hl.cycleID = cc.cycleID AND cc.farmID = f.farmID AND a.allocationID = %s AND f.ownerID = %s""",(allocationID, userID))

        allocation = cursor.fetchone()

        if allocation is None:
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
               SET quantityAllocated = %s, status = %s
               WHERE allocationID = %s""",
            (
                newAllocatedQuantity,
                newAllocationStatus,
                allocationID
            ))

        # Check whether this request still has allocations.
        cursor.execute(
            """SELECT quantityAllocated, status
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
        st.warning(
            "This page is available only to market buyers."
        )
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

    if markets.empty or crops.empty:
        st.info(
            "An administrator must add markets and crops first."
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
                    ["g", "kg", "ton", "box", "piece"],
                    index=1
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
        """SELECT mr.requestID, m.marketName, m.location AS marketLocation, c.cropName, mr.quantityRequested, mr.unit, mr.requestDate, mr.status
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

    for index, request in requests.iterrows():
        requestID = int(request["requestID"])

        allocations = selectData(
            """SELECT quantityAllocated, status
               FROM Allocations
               WHERE requestID = %s""",
            (requestID,)
        )

        allocatedQuantity = 0.0

        if not allocations.empty:
            for _, allocation in allocations.iterrows():
                allocationStatus = str(
                    allocation["status"]
                )

                if allocationStatus != "canceled":
                    allocatedQuantity += float(
                        allocation["quantityAllocated"]
                    )

        receipts = selectData(
            """SELECT receivedQuantity
               FROM DeliveryReceipts
               WHERE requestID = %s""",
            (requestID,)
        )

        receivedQuantity = 0.0

        if not receipts.empty:
            for _, receipt in receipts.iterrows():
                receivedQuantity += float(
                    receipt["receivedQuantity"]
                )

        requests.at[
            index,
            "quantityAllocated"
        ] = allocatedQuantity

        requests.at[
            index,
            "quantityReceived"
        ] = receivedQuantity

        requests.at[
            index,
            "deliveryDifference"
        ] = allocatedQuantity - receivedQuantity

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
            """SELECT status
               FROM MarketRequests
               WHERE requestID = %s AND userID = %s""",
            (requestID, userID)
        )

        request = cursor.fetchone()

        if request is None:
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
    user_id = int(current_user["userID"])

    allocations = selectData(
        """
        SELECT
            a.allocationID,
            a.requestID,
            a.lotID,
            c.cropName,
            a.quantityAllocated,
            mr.unit,
            a.status,
            (
                SELECT IFNULL(SUM(sm.quantity), 0)
                FROM StockMovements sm
                WHERE sm.allocationID = a.allocationID
                  AND sm.movementType = 'delivered'
            ) AS quantityDelivered,
            (
                SELECT IFNULL(SUM(sm.quantity), 0)
                FROM StockMovements sm
                WHERE sm.allocationID = a.allocationID
                  AND sm.movementType = 'canceled'
            ) AS quantityCanceled
        FROM Allocations a
        JOIN MarketRequests mr ON a.requestID = mr.requestID
        JOIN Crops c ON mr.cropID = c.cropID
        WHERE mr.userID = %s
          AND a.status IN ('reserved', 'partiallyDelived')
        HAVING quantityDelivered + quantityCanceled < a.quantityAllocated
        ORDER BY a.allocationDate
        """,
        (user_id,),
    )

    if allocations.empty:
        st.info("There are no outstanding allocated deliveries to confirm.")
        return

    st.dataframe(allocations, use_container_width=True, hide_index=True)
    allocation_options = makeOptions(
        allocations,
        "allocationID",
        [
            "allocationID",
            "requestID",
            "cropName",
            "quantityAllocated",
            "quantityDelivered",
            "quantityCanceled",
            "unit",
        ],
    )

    with st.form("delivery_form"):
        allocation_label = st.selectbox("Allocation received", list(allocation_options.keys()))
        received_quantity = st.number_input("Quantity actually received", min_value=0.01)
        delivery_date = st.date_input("Delivery date", max_value=date.today())
        submitted = st.form_submit_button("Confirm delivery")

        if submitted:
            confirmDelivery(
                allocation_options[allocation_label],
                received_quantity,
                delivery_date,
                user_id,
            )

    receipts = selectData(
        """
        SELECT
            dr.receiptID,
            dr.requestID,
            c.cropName,
            dr.receivedQuantity,
            mr.unit,
            dr.deliveryDate
        FROM DeliveryReceipts dr
        JOIN MarketRequests mr ON dr.requestID = mr.requestID
        JOIN Crops c ON mr.cropID = c.cropID
        WHERE mr.userID = %s
        ORDER BY dr.deliveryDate DESC
        """,
        (user_id,),
    )

    st.subheader("My delivery receipts")
    if not receipts.empty:
        st.dataframe(receipts, use_container_width=True, hide_index=True)


def confirmDelivery(allocation_id, received_quantity, delivery_date, user_id):
    connection = None
    cursor = None

    try:
        connection = getConnection()
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT
                a.requestID,
                a.lotID,
                a.quantityAllocated,
                mr.userID,
                mr.quantityRequested
            FROM Allocations a
            JOIN MarketRequests mr ON a.requestID = mr.requestID
            WHERE a.allocationID = %s
            FOR UPDATE
            """,
            (allocation_id,),
        )
        allocation = cursor.fetchone()

        if allocation is None:
            raise ValueError("Allocation not found.")
        if int(allocation[3]) != user_id:
            raise PermissionError("You cannot confirm another user's delivery.")

        request_id, lot_id, allocated_quantity, _, requested_quantity = allocation

        cursor.execute(
            """
            SELECT
                IFNULL((
                    SELECT SUM(quantity)
                    FROM StockMovements
                    WHERE allocationID = %s
                      AND movementType = 'delivered'
                ), 0),
                IFNULL((
                    SELECT SUM(quantity)
                    FROM StockMovements
                    WHERE allocationID = %s
                      AND movementType = 'canceled'
                ), 0)
            """,
            (allocation_id, allocation_id),
        )
        delivered_and_canceled = cursor.fetchone()
        already_delivered = float(delivered_and_canceled[0])
        already_canceled = float(delivered_and_canceled[1])
        remaining = float(allocated_quantity) - already_delivered - already_canceled

        if received_quantity > remaining:
            raise ValueError(f"Only {remaining:.2f} allocated units remain undelivered.")

        cursor.execute(
            """
            INSERT INTO DeliveryReceipts (requestID, receivedQuantity, deliveryDate)
            VALUES (%s, %s, %s)
            """,
            (request_id, received_quantity, delivery_date),
        )

        cursor.execute(
            """
            INSERT INTO StockMovements
                (lotID, allocationID, movementType, quantity, dateOccurred, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                lot_id,
                allocation_id,
                "delivered",
                received_quantity,
                datetime.now(),
                "Delivery confirmed by user",
            ),
        )

        total_for_allocation = already_delivered + received_quantity
        allocation_status = (
            "delivered"
            if total_for_allocation >= float(allocated_quantity)
            else "partiallyDelived"
        )
        cursor.execute(
            "UPDATE Allocations SET status = %s WHERE allocationID = %s",
            (allocation_status, allocation_id),
        )

        cursor.execute(
            "SELECT SUM(receivedQuantity) FROM DeliveryReceipts WHERE requestID = %s",
            (request_id,),
        )
        total_received = float(cursor.fetchone()[0] or 0)
        request_status = (
            "fullyFulfilled"
            if total_received >= float(requested_quantity)
            else "partiallyFulfilled"
        )
        cursor.execute(
            "UPDATE MarketRequests SET status = %s WHERE requestID = %s",
            (request_status, request_id),
        )

        connection.commit()

        difference = float(allocated_quantity) - total_for_allocation
        st.success(
            f"Delivery recorded. The remaining difference for this allocation is "
            f"{difference:.2f}."
        )
        st.rerun()
    except (ValueError, PermissionError) as error:
        if connection:
            connection.rollback()
        st.warning(str(error))
    except Exception as error:
        if connection:
            connection.rollback()
        st.error(friendlyDatabaseError(error))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# -----------------------------------------------------------------------------
# Administrator pages
# -----------------------------------------------------------------------------

def showAdminReferenceData():
    st.header("Administrator reference data")

    user_tab, crop_tab, market_tab = st.tabs(["Users", "Crops", "Markets"])

    with user_tab:
        users = selectData(
            "SELECT userID, fullname, role, email FROM Users ORDER BY fullname"
        )
        st.dataframe(users, use_container_width=True, hide_index=True)

        with st.form("admin_add_user"):
            fullname = st.text_input("Full name")
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
                if not fullname.strip() or not email.strip() or not password:
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
                            INSERT INTO Users (fullname, role, email, passwordHash)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (
                                fullname.strip(),
                                role,
                                email.strip().lower(),
                                hashPassword(password),
                            ),
                        )
                        user_id = cursor.lastrowid

                        if role == "farmer":
                            cursor.execute(
                                """
                                INSERT INTO Farms (ownerID, farmName, location)
                                VALUES (%s, %s, %s)
                                """,
                                (
                                    user_id,
                                    farm_name.strip(),
                                    farm_location.strip(),
                                ),
                            )

                        connection.commit()
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


def showAdminOperations():
    st.header("Operational records")

    table = st.selectbox(
        "Record type",
        [
            "Farms",
            "CropCycle",
            "HarvestLots",
            "MarketRequests",
            "Allocations",
            "StockMovements",
            "DeliveryReceipts",
        ],
    )

    allowed_queries = {
        "Farms": "SELECT * FROM Farms ORDER BY farmID DESC",
        "CropCycle": "SELECT * FROM CropCycle ORDER BY cycleID DESC",
        "HarvestLots": "SELECT * FROM HarvestLots ORDER BY lotID DESC",
        "MarketRequests": "SELECT * FROM MarketRequests ORDER BY requestID DESC",
        "Allocations": "SELECT * FROM Allocations ORDER BY allocationID DESC",
        "StockMovements": "SELECT * FROM StockMovements ORDER BY movementID DESC",
        "DeliveryReceipts": "SELECT * FROM DeliveryReceipts ORDER BY receiptID DESC",
    }

    records = selectData(allowed_queries[table])
    if records.empty:
        st.info("There are no records in this table.")
    else:
        st.dataframe(records, use_container_width=True, hide_index=True)


# -----------------------------------------------------------------------------
# Read-only Gemini assistant
# -----------------------------------------------------------------------------

def showAssistant(current_user):
    st.header("Database assistant")
    st.caption(
        "The assistant receives results from approved read-only SELECT queries. "
        "It cannot create, update or delete records."
    )

    question = st.text_input(
        "Ask about availability, expected harvests, requests or deliveries",
        placeholder="For example: Which crops are currently available?",
    )

    if st.button("Ask assistant"):
        if not question.strip():
            st.warning("Enter a question first.")
            return

        user_id = int(current_user["userID"])
        role = current_user["role"]

        availability = selectData(availabilityQuery(), show_error=False).head(100)
        expected = selectData(
            """
            SELECT
                f.farmName,
                c.cropName,
                cc.expectedYield AS expectedYieldKg,
                cc.expectedHarvestDate
            FROM CropCycle cc
            JOIN Farms f ON cc.farmID = f.farmID
            JOIN Crops c ON cc.cropID = c.cropID
            ORDER BY cc.expectedHarvestDate
            LIMIT 100
            """,
            show_error=False,
        )

        if role == "user":
            requests = selectData(
                """
                SELECT mr.requestID, m.marketName, c.cropName,
                       mr.quantityRequested, mr.unit, mr.status,
                       (
                           SELECT SUM(dr.receivedQuantity)
                           FROM DeliveryReceipts dr
                           WHERE dr.requestID = mr.requestID
                       ) AS receivedQuantity
                FROM MarketRequests mr
                JOIN Markets m ON mr.marketID = m.marketID
                JOIN Crops c ON mr.cropID = c.cropID
                WHERE mr.userID = %s
                ORDER BY mr.requestID DESC
                LIMIT 100
                """,
                (user_id,),
                show_error=False,
            )
        elif role == "farmer":
            requests = selectData(
                """
                SELECT a.allocationID, a.requestID, a.lotID,
                       a.quantityAllocated, a.status
                FROM Allocations a
                JOIN HarvestLots hl ON a.lotID = hl.lotID
                JOIN CropCycle cc ON hl.cycleID = cc.cycleID
                JOIN Farms f ON cc.farmID = f.farmID
                WHERE f.ownerID = %s
                ORDER BY a.allocationID DESC
                LIMIT 100
                """,
                (user_id,),
                show_error=False,
            )
        else:
            requests = selectData(
                """
                SELECT mr.requestID, m.marketName, c.cropName,
                       mr.quantityRequested, mr.unit, mr.status
                FROM MarketRequests mr
                JOIN Markets m ON mr.marketID = m.marketID
                JOIN Crops c ON mr.cropID = c.cropID
                ORDER BY mr.requestID DESC
                LIMIT 100
                """,
                show_error=False,
            )

        if role == "user" and not requests.empty:
            requests["receivedQuantity"] = pd.to_numeric(
                requests["receivedQuantity"]
            ).fillna(0)

        context = {
            "availability": availability.to_dict(orient="records"),
            "expected_harvests": expected.to_dict(orient="records"),
            "role_records": requests.to_dict(orient="records"),
        }

        prompt = f"""
You answer questions about a fictional farm-market database.
Use only the supplied records. Do not invent missing facts.
If the question is ambiguous, ask for the missing crop, farm, market, date, or unit.
If the records cannot answer it, say so.
Keep expected harvest separate from saleable availability.
Always mention units when they are available. Never produce SQL.

User role: {role}
Question: {question}
Approved database records: {context}
"""

        try:
            client = getGeminiClient()
            response = client.models.generate_content(
                model=getModelName(),
                contents=prompt,
            )
            st.write(response.text.strip())

            with st.expander("Supporting records"):
                st.write("Available harvested stock")
                st.dataframe(availability, use_container_width=True, hide_index=True)
                st.write("Expected harvests")
                st.dataframe(expected, use_container_width=True, hide_index=True)
                st.write("Role-related requests or allocations")
                st.dataframe(requests, use_container_width=True, hide_index=True)
        except Exception as error:
            st.error(f"The assistant could not answer: {error}")


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
        st.session_state.currentUser = None
        st.rerun()

    # Pages allowed for each role
    rolePages = {
        "farmer": [
            "Dashboard",
            "Farm management",
            "Harvest management",
            "Requests and allocations",
            "Images",
        ],

        "user": [
            "Search produce",
            "My requests",
            "Deliveries",
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
        showAdminReferenceData()

    elif page == "Operational records":
        showAdminOperations()

    elif page == "Assistant":
        showAssistant(currentUser)


if __name__ == "__main__":
    main()