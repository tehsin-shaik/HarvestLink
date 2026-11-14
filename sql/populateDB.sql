USE DBProjGroup3;


INSERT INTO Users (userID, fname, lname, role, email, passwordHash) VALUES
(1, 'Raghad', 'Elajab', 'farmer', 'raghadelajab@gmail.com', '894c6ccbfea998515381ab1771a8ac92cd2a12a4f306eb6469874eb7d1cf8b8f'),
(2, 'Tehsin', 'Shaik', 'farmer', 'tehsinshaik@gmail.com', '62eeca2f0bab96209afd671e05958bc8fe211a3f58b8fb83b049c83d52fd598b'),
(3, 'Ayesha', 'Almheiri', 'farmer', 'ayeshaalmheiri@gmail.com', '3afeb453ed5564c9ddcd28af3be53eec2a0291d5471bf0e1336cfdd0905c72ea'),
(4, 'Ahmed', 'Alshehhi', 'user', 'ahmed@gmail.com', 'df8e7c6127e87fb73d95121c1d2928b743cab6296ba2fb77ff3a368c8f895c06'),
(5, 'Mohammed', 'Ahmed', 'user', 'mohammed@gmail.com', 'ca80f52c488a05698d33658b288491b99b21ecce8c6780253a40799e5fea7485'),
(6, 'Nada', 'Elsokkary', 'admin', 'admin@gmail.com', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9');


INSERT INTO Farms (farmID, farmName, location) VALUES
(1, 'Abu Dhabi Farms', 'Abu Dhabi'),
(2, 'Dubai Farms', 'Dubai'),
(3, 'Ajman Farms', 'Ajman'),
(4, 'Al Ain Oasis Farm', 'Al Ain');


INSERT INTO OwnedBy (userID, farmID) VALUES
(1, 1),
(1, 4),
(2, 2),
(3, 3);


INSERT INTO Markets (marketID, marketName, location) VALUES
(1, 'Abu Dhabi Market', 'Abu Dhabi'),
(2, 'Dubai Market', 'Dubai'),
(3, 'Ajman Market', 'Ajman'),
(4, 'Al Ain Market', 'Al Ain');


INSERT INTO Crops (cropID, cropName) VALUES
(1, 'Carrots'),
(2, 'Strawberries'),
(3, 'Apples'),
(4, 'Watermelon'),
(5, 'Papaya');


INSERT INTO CropCycle (cycleID, farmID, cropID, expectedUnit, expectedYield, expectedHarvestDate, status) VALUES
(1, 1, 1, 'kg', 1000.00, '2026-11-05', 0),
(2, 1, 2, 'kg', 600.00, '2026-11-06', 0),
(3, 2, 3, 'kg', 1200.00, '2026-11-07', 0),
(4, 2, 5, 'kg', 800.00, '2026-11-08', 0),
(5, 2, 1, 'ton', 0.90, '2026-11-09', 0),
(6, 3, 2, 'g', 700000.00, '2026-11-10', 0),
(7, 3, 4, 'kg', 1400.00, '2026-11-09', 0),
(8, 2, 3, 'g', 300000.00, '2026-11-12', 0),
(9, 3, 2, 'g', 180000.00, '2026-11-03', 0),
(10, 1, 1, 'ton', 0.25, '2026-10-30', 0),
(11, 1, 4, 'ton', 1.50, '2026-11-22', 1),
(12, 3, 5, 'kg', 600.00, '2026-11-20', 1),
(13, 2, 1, 'kg', 900.00, '2026-11-17', 1),
(14, 1, 3, 'kg', 500.00, '2026-11-18', 1),
(15, 4, 5, 'kg', 300.00, '2026-11-13', 0),
(16, 4, 2, 'kg', 450.00, '2026-11-25', 1);


-- Lot 10 has no stock remaining, so its status is 0.
-- Lot 11 demonstrates an optional NULL sell-by date.
INSERT INTO HarvestLots (lotID, cycleID, quantityHarvested, unit, harvestDate, sellByDate, status) VALUES
(1, 1, 500.00, 'kg', '2026-11-05', '2026-11-25', 1),
(2, 2, 300.00, 'kg', '2026-11-06', '2026-11-20', 1),
(3, 3, 600.00, 'kg', '2026-11-07', '2026-12-05', 1),
(4, 4, 400.00, 'kg', '2026-11-08', '2026-11-22', 1),
(5, 5, 0.45, 'ton', '2026-11-09', '2026-11-28', 1),
(6, 6, 350000.00, 'g', '2026-11-10', '2026-11-19', 1),
(7, 7, 700.00, 'kg', '2026-11-09', '2026-11-25', 1),
(8, 8, 300000.00, 'g', '2026-11-12', '2026-12-10', 1),
(9, 9, 180000.00, 'g', '2026-11-03', '2026-11-21', 1),
(10, 10, 0.25, 'ton', '2026-10-30', '2026-11-10', 0),
(11, 15, 250.00, 'kg', '2026-11-13', NULL, 1),
(12, 2, 200.00, 'kg', '2026-11-14', '2026-11-28', 1);

INSERT INTO MarketRequests (requestID, userID, marketID, cropID, quantityRequested, unit, requestDate, reqStatus) VALUES
(1, 4, 1, 1, 120.00, 'kg', '2026-11-05 09:00:00', 'partiallyFulfilled'),
(2, 4, 2, 3, 200.00, 'kg', '2026-11-07 10:00:00', 'fullyFulfilled'),
(3, 4, 3, 2, 90.00, 'kg', '2026-11-10 11:00:00', 'pending'),
(4, 4, 4, 4, 250.00, 'kg', '2026-11-10 09:30:00', 'partiallyFulfilled'),
(5, 5, 2, 5, 100.00, 'kg', '2026-11-08 14:00:00', 'partiallyFulfilled'),
(6, 5, 1, 2, 80000.00, 'g', '2026-11-11 15:00:00', 'partiallyFulfilled'),
(7, 5, 3, 4, 300.00, 'kg', '2026-11-09 08:00:00', 'partiallyFulfilled'),
(8, 5, 2, 1, 150.00, 'kg', '2026-11-06 13:00:00', 'canceled'),
(9, 4, 1, 3, 50000.00, 'g', '2026-11-13 10:00:00', 'partiallyFulfilled');


INSERT INTO RequestStatusHistory (requestID, oldStatus, newStatus, changedAt, changedBy, reason) VALUES
(1, NULL, 'pending', '2026-11-05 09:00:00', 4, 'Market request created'),
(1, 'pending', 'partiallyFulfilled', '2026-11-06 09:00:00', 1, 'Produce allocated by farmer'),
(2, NULL, 'pending', '2026-11-07 10:00:00', 4, 'Market request created'),
(2, 'pending', 'partiallyFulfilled', '2026-11-08 10:00:00', 2, 'Produce allocated by farmer'),
(2, 'partiallyFulfilled', 'fullyFulfilled', '2026-11-09 16:00:00', 4, 'Delivery confirmed by market buyer'),
(3, NULL, 'pending', '2026-11-10 11:00:00', 4, 'Market request created'),
(4, NULL, 'pending', '2026-11-10 09:30:00', 4, 'Market request created'),
(4, 'pending', 'partiallyFulfilled', '2026-11-11 09:00:00', 3, 'Produce allocated by farmer'),
(5, NULL, 'pending', '2026-11-08 14:00:00', 5, 'Market request created'),
(5, 'pending', 'partiallyFulfilled', '2026-11-09 14:00:00', 2, 'Produce allocated by farmer'),
(6, NULL, 'pending', '2026-11-11 15:00:00', 5, 'Market request created'),
(6, 'pending', 'partiallyFulfilled', '2026-11-12 09:00:00', 3, 'Produce allocated by farmer'),
(7, NULL, 'pending', '2026-11-09 08:00:00', 5, 'Market request created'),
(7, 'pending', 'partiallyFulfilled', '2026-11-10 08:00:00', 3, 'Produce allocated by farmer'),
(8, NULL, 'pending', '2026-11-06 13:00:00', 5, 'Market request created'),
(8, 'pending', 'canceled', '2026-11-07 13:00:00', 5, 'Request no longer needed'),
(9, NULL, 'pending', '2026-11-13 10:00:00', 4, 'Market request created'),
(9, 'pending', 'partiallyFulfilled', '2026-11-13 11:00:00', 2, 'Produce allocated by farmer');



INSERT INTO Allocations (allocationID, requestID, lotID, allocatedBy, quantityAllocated, allocationDate, allocStatus) VALUES
(1, 1, 1, 1, 120.00, '2026-11-06 09:00:00', 'delivered'),
(2, 2, 3, 2, 200.00, '2026-11-08 10:00:00', 'delivered'),
(3, 4, 7, 3, 250.00, '2026-11-11 09:00:00', 'delivered'),
(4, 5, 4, 2, 100.00, '2026-11-09 14:00:00', 'delivered'),
(5, 7, 7, 3, 300.00, '2026-11-10 08:00:00', 'delivered'),
(6, 6, 9, 3, 80000.00, '2026-11-12 09:00:00', 'delivered'),
(7, 9, 8, 2, 50000.00, '2026-11-13 11:00:00', 'reserved');


INSERT INTO StockMovements (lotID, allocationID, movementType, quantity, dateOccurred, reason, performedBy) VALUES
(1, NULL, 'added', 500.00, '2026-11-05 12:00:00', 'Initial carrot harvest', 1),
(2, NULL, 'added', 300.00, '2026-11-06 12:00:00', 'Initial strawberry harvest', 1),
(3, NULL, 'added', 600.00, '2026-11-07 12:00:00', 'Initial apple harvest', 2),
(4, NULL, 'added', 400.00, '2026-11-08 12:00:00', 'Initial papaya harvest', 2),
(5, NULL, 'added', 0.45, '2026-11-09 12:00:00', 'Initial carrot harvest', 2),
(6, NULL, 'added', 350000.00, '2026-11-10 12:00:00', 'Initial strawberry harvest', 3),
(7, NULL, 'added', 700.00, '2026-11-09 12:00:00', 'Initial watermelon harvest', 3),
(8, NULL, 'added', 300000.00, '2026-11-12 12:00:00', 'Initial apple harvest', 2),
(9, NULL, 'added', 180000.00, '2026-11-03 12:00:00', 'Initial strawberry harvest', 3),
(10, NULL, 'added', 0.25, '2026-10-30 12:00:00', 'Initial carrot harvest', 1),
(11, NULL, 'added', 250.00, '2026-11-13 12:00:00', 'Initial Al Ain papaya harvest', 1),
(12, NULL, 'added', 200.00, '2026-11-14 12:00:00', 'Initial strawberry harvest', 1);

INSERT INTO StockMovements (lotID, allocationID, movementType, quantity, dateOccurred, reason, performedBy) VALUES
(1, 1, 'reserved', 120.00, '2026-11-06 09:00:00', 'Reserved for request 1', 1),
(3, 2, 'reserved', 200.00, '2026-11-08 10:00:00', 'Reserved for request 2', 2),
(7, 3, 'reserved', 250.00, '2026-11-11 09:00:00', 'Reserved for request 4', 3),
(4, 4, 'reserved', 100.00, '2026-11-09 14:00:00', 'Reserved for request 5', 2),
(7, 5, 'reserved', 300.00, '2026-11-10 08:00:00', 'Reserved for request 7', 3),
(9, 6, 'reserved', 80000.00, '2026-11-12 09:00:00', 'Reserved for request 6', 3),
(8, 7, 'reserved', 50000.00, '2026-11-13 11:00:00', 'Reserved for request 9', 2);


INSERT INTO StockMovements (lotID, allocationID, movementType, quantity, dateOccurred, reason, performedBy) VALUES
(1, 1, 'delivered', 92.00, '2026-11-07 16:00:00', 'Delivery confirmed by market buyer', 4),
(1, 1, 'canceled', 28.00, '2026-11-07 16:00:00', 'Undelivered quantity released', 4),
(3, 2, 'delivered', 200.00, '2026-11-09 16:00:00', 'Delivery confirmed by market buyer', 4),
(7, 3, 'delivered', 225.00, '2026-11-12 16:00:00', 'Delivery confirmed by market buyer', 4),
(7, 3, 'canceled', 25.00, '2026-11-12 16:00:00', 'Undelivered quantity released', 4),
(4, 4, 'delivered', 90.00, '2026-11-10 17:00:00', 'Delivery confirmed by market buyer', 5),
(4, 4, 'canceled', 10.00, '2026-11-10 17:00:00', 'Undelivered quantity released', 5),
(7, 5, 'delivered', 250.00, '2026-11-13 17:00:00', 'Delivery confirmed by market buyer', 5),
(9, 6, 'delivered', 75000.00, '2026-11-14 10:00:00', 'Delivery confirmed by market buyer', 5),
(7, 5, 'canceled', 50.00, '2026-11-13 17:00:00', 'Undelivered quantity released', 5),
(9, 6, 'canceled', 5000.00, '2026-11-14 10:00:00', 'Undelivered quantity released', 5);

INSERT INTO StockMovements (lotID, allocationID, movementType, quantity, dateOccurred, reason, performedBy) VALUES
(5, NULL, 'loss', 0.05, '2026-11-12 08:00:00', 'Damaged carrots removed', 2),
(6, NULL, 'loss', 25000.00, '2026-11-12 08:30:00', 'Bruised strawberries removed', 3),
(9, NULL, 'loss', 30000.00, '2026-11-12 09:00:00', 'Strawberry spoilage', 3),
(10, NULL, 'loss', 0.25, '2026-11-11 09:30:00', 'Expired carrot lot removed', 1),
(4, NULL, 'correction_added', 10.00, '2026-11-12 10:00:00', 'Approved count correction after recount', 2);


INSERT INTO DeliveryReceipts (requestID, allocationID, expectedQuantity, receivedQuantity, discrepancyQuantity, unit, deliveryDate, confirmedBy) VALUES
(1, 1, 120.00, 92.00, 28.00, 'kg', '2026-11-07 16:00:00', 4),
(2, 2, 200.00, 200.00, 0.00, 'kg', '2026-11-09 16:00:00', 4),
(4, 3, 250.00, 225.00, 25.00, 'kg', '2026-11-12 16:00:00', 4),
(5, 4, 100.00, 90.00, 10.00, 'kg', '2026-11-10 17:00:00', 5),
(6, 6, 80000.00, 75000.00, 5000.00, 'g', '2026-11-14 10:00:00', 5),
(7, 5, 300.00, 250.00, 50.00, 'kg', '2026-11-13 17:00:00', 5);


INSERT INTO HarvestLotImages (imageNumber, lotID, imageName) VALUES
(1, 1, 'lot_1_1_carrots.png'),
(1, 2, 'lot_2_1_strawberries.png'),
(1, 3, 'lot_3_1_apples.png'),
(1, 4, 'lot_4_1_papaya.png'),
(1, 7, 'lot_7_1_watermelon.png');


INSERT INTO UserActivityLog (userID, actionType, actionDate, details) VALUES
(1, 'login', '2026-11-13 08:00:00', 'User signed in'),
(1, 'harvest_recorded', '2026-11-13 12:00:00', 'Recorded harvest lot 11'),
(1, 'logout', '2026-11-13 13:00:00', 'User signed out after 05:00:00'),
(2, 'login', '2026-11-13 09:00:00', 'User signed in'),
(2, 'allocation_created', '2026-11-13 11:00:00', 'Created allocation 7 for request 9'),
(2, 'logout', '2026-11-13 12:00:00', 'User signed out after 03:00:00'),
(4, 'login', '2026-11-13 15:30:00', 'User signed in'),
(4, 'delivery_confirmed', '2026-11-13 16:00:00', 'Confirmed delivery with recorded discrepancy'),
(4, 'logout', '2026-11-13 16:30:00', 'User signed out after 01:00:00'),
(6, 'login', '2026-11-14 08:00:00', 'Administrator signed in'),
(5, 'delivery_confirmed', '2026-11-14 10:00:00', 'Confirmed allocation 6; discrepancy 5000 g'),
(6, 'logout', '2026-11-14 09:00:00', 'Administrator signed out after 01:00:00');