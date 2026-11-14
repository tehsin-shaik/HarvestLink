CREATE SCHEMA IF NOT EXISTS DBProjGroup3;
CREATE DATABASE IF NOT EXISTS DBProjGroup3;
USE DBProjGroup3;

-- Admins can manage which units are available in the app.
CREATE TABLE IF NOT EXISTS Units (
    unitName VARCHAR(20) PRIMARY KEY,
    isActive BOOLEAN NOT NULL DEFAULT 1,
    CHECK (isActive IN (0, 1))
);

CREATE TABLE Users(
    userID INT AUTO_INCREMENT PRIMARY KEY,
    fname VARCHAR(20) NOT NULL,
    lname VARCHAR(20) NOT NULL,
    role VARCHAR(20) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    passwordHash VARCHAR(255) NOT NULL,
    CHECK (role IN ('farmer', 'user', 'admin'))
);


CREATE TABLE Farms(
    farmID INT AUTO_INCREMENT PRIMARY KEY,
    farmName VARCHAR(100) NOT NULL,
    location VARCHAR(100) NOT NULL
);


CREATE TABLE OwnedBy(
    userID INT NOT NULL,
    farmID INT NOT NULL,
    PRIMARY KEY (userID, farmID),
    FOREIGN KEY (userID) REFERENCES Users(userID),
    FOREIGN KEY (farmID) REFERENCES Farms(farmID)
);


CREATE TABLE Crops(
    cropID INT AUTO_INCREMENT PRIMARY KEY,
    cropName VARCHAR(100) NOT NULL UNIQUE
);


CREATE TABLE CropCycle(
    cycleID INT AUTO_INCREMENT PRIMARY KEY,
    farmID INT NOT NULL,
    cropID INT NOT NULL,
    expectedUnit VARCHAR(20) NOT NULL DEFAULT 'kg',
    expectedYield DECIMAL(10,2) NOT NULL,
    expectedHarvestDate DATE NOT NULL,
    status BOOLEAN NOT NULL DEFAULT 1,
    FOREIGN KEY (farmID) REFERENCES Farms(farmID),
    FOREIGN KEY (cropID) REFERENCES Crops(cropID),
    CHECK (expectedYield > 0)
);


CREATE TABLE HarvestLots(
    lotID INT AUTO_INCREMENT PRIMARY KEY,
    cycleID INT NOT NULL,
    quantityHarvested DECIMAL(10,2) NOT NULL,
    unit VARCHAR(20) NOT NULL,
    harvestDate DATE NOT NULL,
    sellByDate DATE,
    status BOOLEAN NOT NULL DEFAULT 1,
    FOREIGN KEY (cycleID) REFERENCES CropCycle(cycleID),
    CHECK (quantityHarvested > 0),
    CHECK (
        sellByDate IS NULL
        OR sellByDate >= harvestDate
    )
);


CREATE TABLE HarvestLotImages(
    imageNumber INT NOT NULL,
    lotID INT NOT NULL,
    imageName VARCHAR(255) NOT NULL,
    PRIMARY KEY (lotID, imageNumber),
    FOREIGN KEY (lotID) REFERENCES HarvestLots(lotID)
);


CREATE TABLE Markets(
    marketID INT AUTO_INCREMENT PRIMARY KEY,
    marketName VARCHAR(100) NOT NULL,
    location VARCHAR(255) NOT NULL
);


CREATE TABLE MarketRequests(
    requestID INT AUTO_INCREMENT PRIMARY KEY,
    userID INT NOT NULL,
    marketID INT NOT NULL,
    cropID INT NOT NULL,
    quantityRequested DECIMAL(10,2) NOT NULL,
    unit VARCHAR(20) NOT NULL,
    requestDate DATETIME NOT NULL,
    reqStatus VARCHAR(30) NOT NULL,
    FOREIGN KEY (userID) REFERENCES Users(userID),
    FOREIGN KEY (marketID) REFERENCES Markets(marketID),
    FOREIGN KEY (cropID) REFERENCES Crops(cropID),
    CHECK (quantityRequested > 0),
    CHECK (reqStatus IN ('pending', 'partiallyFulfilled', 'fullyFulfilled', 'canceled'))
);


CREATE TABLE RequestStatusHistory(
    historyID INT AUTO_INCREMENT PRIMARY KEY,
    requestID INT NOT NULL,
    oldStatus VARCHAR(30),
    newStatus VARCHAR(30) NOT NULL,
    changedAt DATETIME NOT NULL,
    changedBy INT NOT NULL,
    reason VARCHAR(500),
    FOREIGN KEY (requestID) REFERENCES MarketRequests(requestID),
    FOREIGN KEY (changedBy) REFERENCES Users(userID)
);


CREATE TABLE Allocations(
    allocationID INT AUTO_INCREMENT PRIMARY KEY,
    requestID INT NOT NULL,
    lotID INT NOT NULL,
    allocatedBy INT NOT NULL,
    quantityAllocated DECIMAL(10,2) NOT NULL,
    allocationDate DATETIME NOT NULL,
    allocStatus VARCHAR(30) NOT NULL,
    FOREIGN KEY (requestID) REFERENCES MarketRequests(requestID),
    FOREIGN KEY (lotID) REFERENCES HarvestLots(lotID),
    FOREIGN KEY (allocatedBy) REFERENCES Users(userID),
    CHECK (quantityAllocated > 0),
    CHECK (allocStatus IN ('reserved', 'partiallyDelivered', 'delivered', 'canceled'))
);


CREATE TABLE StockMovements(
    movementID INT AUTO_INCREMENT PRIMARY KEY,
    lotID INT NOT NULL,
    allocationID INT,
    movementType VARCHAR(30) NOT NULL,
	quantity DECIMAL(10,2) NOT NULL,
	unit VARCHAR(20) NOT NULL,
	dateOccurred DATETIME NOT NULL,
    reason VARCHAR(500),
    performedBy INT NOT NULL,
    FOREIGN KEY (lotID) REFERENCES HarvestLots(lotID),
    FOREIGN KEY (allocationID) REFERENCES Allocations(allocationID),
    FOREIGN KEY (performedBy) REFERENCES Users(userID),
    CHECK (quantity > 0),
    CHECK (movementType IN ('added', 'reserved', 'canceled', 'delivered', 'loss', 'correction_added', 'correction_removed'))
);


CREATE TABLE DeliveryReceipts(
    receiptID INT AUTO_INCREMENT PRIMARY KEY,
    requestID INT NOT NULL,
    allocationID INT NOT NULL UNIQUE,
    expectedQuantity DECIMAL(10,2) NOT NULL,
    receivedQuantity DECIMAL(10,2) NOT NULL,
    discrepancyQuantity DECIMAL(10,2) NOT NULL,
    unit VARCHAR(20) NOT NULL,
    deliveryDate DATETIME NOT NULL,
    confirmedBy INT NOT NULL,
    FOREIGN KEY (requestID) REFERENCES MarketRequests(requestID),
    FOREIGN KEY (allocationID) REFERENCES Allocations(allocationID),
    FOREIGN KEY (confirmedBy) REFERENCES Users(userID),
    CHECK (expectedQuantity > 0),
    CHECK (receivedQuantity >= 0),
    CHECK (discrepancyQuantity = expectedQuantity - receivedQuantity)
);


CREATE TABLE UserActivityLog(
    activityID INT AUTO_INCREMENT PRIMARY KEY,
    userID INT NOT NULL,
    actionType VARCHAR(50) NOT NULL,
    actionDate DATETIME NOT NULL,
    details VARCHAR(500),
    FOREIGN KEY (userID) REFERENCES Users(userID)
);

