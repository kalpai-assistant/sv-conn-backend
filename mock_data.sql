-- Seed Mock Data for ServiceConnect

-- Clear existing data to prevent unique constraint violations
TRUNCATE TABLE booking_status_logs CASCADE;
TRUNCATE TABLE payments CASCADE;
TRUNCATE TABLE bookings CASCADE;
TRUNCATE TABLE addresses CASCADE;
TRUNCATE TABLE provider_services CASCADE;
TRUNCATE TABLE services CASCADE;
TRUNCATE TABLE categories CASCADE;
TRUNCATE TABLE provider_profiles CASCADE;
TRUNCATE TABLE users CASCADE;

-- Users (Admin, Consumers, Providers)
INSERT INTO users (id, phone, name, email, role, is_active) VALUES
('b409935a-b247-41fc-9005-608975ae93db', '9999999999', 'Admin User', 'admin@svcconnect.com', 'ADMIN', true),
('19d1d9b6-6a45-4999-8585-28b3c01f5295', '9876543200', 'Arjun Mehta', 'arjun@gmail.com', 'CONSUMER', true),
('f3068104-d3b9-48fb-a809-c12713d888ae', '9876543201', 'Priya Singh', 'priya@gmail.com', 'CONSUMER', true),
('ed3e8415-378a-4ea6-97a7-896476f1ee82', '9876543202', 'Rahul Dev', 'rahul@gmail.com', 'CONSUMER', true),
('1e54a653-94b6-4325-afa7-60f58c2a3486', '9876543210', 'Rajesh Kumar', 'rajesh@svcconnect.com', 'PROVIDER', true),
('bbcd0d11-824a-4c72-9649-6e3a195873e2', '9876543211', 'Anita Sharma', 'anita@svcconnect.com', 'PROVIDER', true),
('952e3efd-562d-4f97-9717-f5a7eb256e12', '9876543212', 'Mohammed Irfan', 'mohammed@svcconnect.com', 'PROVIDER', true),
('3de586fa-6fd9-421c-8e44-3d7076a97426', '9876543213', 'Lakshmi Devi', 'lakshmi@svcconnect.com', 'PROVIDER', true),
('c83e3888-a4a0-45b9-a0ed-6a65b26f7fe9', '9876543214', 'Deepa Nair', 'deepa@svcconnect.com', 'PROVIDER', true);

-- Provider Profiles (with PostGIS location in Bangalore)
INSERT INTO provider_profiles (id, user_id, service_area_radius_km, avg_rating, total_completed_jobs, kyc_status, is_online, service_area_center) VALUES
('fc5443a8-544e-489a-957e-3f826ad321d8', '1e54a653-94b6-4325-afa7-60f58c2a3486', 15, 4.8, 234, 'VERIFIED', true, ST_SetSRID(ST_MakePoint(77.5946, 12.9716), 4326)),
('2b519a6f-f27a-4786-8250-cde1ea58c9bb', 'bbcd0d11-824a-4c72-9649-6e3a195873e2', 10, 4.9, 189, 'VERIFIED', true, ST_SetSRID(ST_MakePoint(77.6412, 12.9784), 4326)),
('54ac6f0d-68b8-4455-9029-be7aa0ee2d30', '952e3efd-562d-4f97-9717-f5a7eb256e12', 20, 4.6, 156, 'VERIFIED', true, ST_SetSRID(ST_MakePoint(77.6101, 12.9250), 4326)),
('29b1d12f-d3a0-45cd-bf6b-1b2e8df07bb6', '3de586fa-6fd9-421c-8e44-3d7076a97426', 12, 4.7, 312, 'VERIFIED', false, ST_SetSRID(ST_MakePoint(77.5505, 13.0068), 4326)),
('eae9cf9c-d9a1-4c03-8cc3-0a81d8055509', 'c83e3888-a4a0-45b9-a0ed-6a65b26f7fe9', 8, 4.5, 98, 'PENDING', true, ST_SetSRID(ST_MakePoint(77.6200, 12.9350), 4326));

-- Categories
INSERT INTO categories (id, name, slug, icon_url, sort_order, is_active) VALUES
('c593ee69-115a-4dad-a611-a45530d83bb5', 'Cleaning', 'cleaning', 'Sparkles', 1, true),
('695f5e73-df27-469f-a4a2-19ce0c77c57b', 'Plumbing', 'plumbing', 'Wrench', 2, true),
('4f4c1aa2-32ff-4cb6-b0f3-e0580778b8ea', 'Electrical', 'electrical', 'Zap', 3, true),
('ee8b552a-df93-4519-8028-58e5fb27c167', 'Salon', 'salon', 'Scissors', 4, true);

-- Services
INSERT INTO services (id, category_id, name, description, base_price, duration_minutes, is_active) VALUES
('572cc978-e1e6-49b6-b5b1-876d1c25aef0', 'c593ee69-115a-4dad-a611-a45530d83bb5', 'Full Home Deep Cleaning', 'Professional deep cleaning of entire home.', 1999.0, 180, true),
('5d307f84-a903-46b8-aff3-e8e1afd35615', 'c593ee69-115a-4dad-a611-a45530d83bb5', 'Bathroom Deep Cleaning', 'Thorough cleaning of bathrooms including tiles.', 599.0, 60, true),
('ffe0462a-25b0-4aa6-8243-965572368c65', 'c593ee69-115a-4dad-a611-a45530d83bb5', 'Sofa Cleaning', 'Dry vacuuming and wet shampooing of sofa.', 799.0, 90, true),
('1e9d6da5-200f-4cdd-92cb-e1af2c08640c', '695f5e73-df27-469f-a4a2-19ce0c77c57b', 'Pipe Leak Repair', 'Fix leaking pipes, joints and faucets.', 349.0, 45, true),
('02628084-d1c8-4679-ac01-21258075539c', '695f5e73-df27-469f-a4a2-19ce0c77c57b', 'Tap & Mixer Installation', 'Installation of new taps and mixers.', 249.0, 30, true),
('51f27cf7-6194-4611-bfc4-fe4a1e3e97f8', '695f5e73-df27-469f-a4a2-19ce0c77c57b', 'Water Tank Cleaning', 'Mechanized cleaning of overhead water tanks.', 999.0, 120, true),
('a5a25f09-41de-4f76-a108-2230d2c23944', '4f4c1aa2-32ff-4cb6-b0f3-e0580778b8ea', 'Fan Installation', 'Ceiling or wall fan installation with wiring.', 299.0, 45, true),
('1f5a5780-06bb-4559-9c2c-5f222333daa5', '4f4c1aa2-32ff-4cb6-b0f3-e0580778b8ea', 'Switchboard Repair', 'Repair or replacement of switchboards.', 199.0, 30, true),
('be3eac6d-b4f5-4d2b-aa58-da7b5dc05af2', '4f4c1aa2-32ff-4cb6-b0f3-e0580778b8ea', 'Inverter Installation', 'Setup and connection of home inverter.', 599.0, 60, true),
('ac5a39d2-7f13-47f6-b5dd-d56e28584be9', 'ee8b552a-df93-4519-8028-58e5fb27c167', 'Womens Haircut', 'Professional haircut at your doorstep.', 499.0, 45, true),
('38274b5e-bf10-4388-886a-a44dd2a22f79', 'ee8b552a-df93-4519-8028-58e5fb27c167', 'Bridal Makeup', 'Complete bridal makeup package with HD finish.', 4999.0, 180, true),
('4324d1b9-37b2-426c-b8fc-e7958e60096d', 'ee8b552a-df93-4519-8028-58e5fb27c167', 'Manicure & Pedicure', 'Relaxing mani-pedi spa treatment.', 899.0, 90, true);

-- Provider Services (Subscriptions)
INSERT INTO provider_services (id, provider_id, service_id) VALUES
(gen_random_uuid(), '1e54a653-94b6-4325-afa7-60f58c2a3486', '572cc978-e1e6-49b6-b5b1-876d1c25aef0'),
(gen_random_uuid(), '1e54a653-94b6-4325-afa7-60f58c2a3486', '5d307f84-a903-46b8-aff3-e8e1afd35615'),
(gen_random_uuid(), '1e54a653-94b6-4325-afa7-60f58c2a3486', 'ffe0462a-25b0-4aa6-8243-965572368c65'),
(gen_random_uuid(), 'bbcd0d11-824a-4c72-9649-6e3a195873e2', '1e9d6da5-200f-4cdd-92cb-e1af2c08640c'),
(gen_random_uuid(), 'bbcd0d11-824a-4c72-9649-6e3a195873e2', '02628084-d1c8-4679-ac01-21258075539c'),
(gen_random_uuid(), '952e3efd-562d-4f97-9717-f5a7eb256e12', 'a5a25f09-41de-4f76-a108-2230d2c23944'),
(gen_random_uuid(), '952e3efd-562d-4f97-9717-f5a7eb256e12', '1f5a5780-06bb-4559-9c2c-5f222333daa5'),
(gen_random_uuid(), '3de586fa-6fd9-421c-8e44-3d7076a97426', 'ac5a39d2-7f13-47f6-b5dd-d56e28584be9'),
(gen_random_uuid(), '3de586fa-6fd9-421c-8e44-3d7076a97426', '38274b5e-bf10-4388-886a-a44dd2a22f79'),
(gen_random_uuid(), 'c83e3888-a4a0-45b9-a0ed-6a65b26f7fe9', '5d307f84-a903-46b8-aff3-e8e1afd35615'),
(gen_random_uuid(), 'c83e3888-a4a0-45b9-a0ed-6a65b26f7fe9', '51f27cf7-6194-4611-bfc4-fe4a1e3e97f8');

-- Consumer Addresses (Bangalore locations)
INSERT INTO addresses (id, user_id, label, full_address, location, is_default) VALUES
('2b96c23e-7b8c-4486-a3a5-dc6509cdee60', '19d1d9b6-6a45-4999-8585-28b3c01f5295', 'home', '42, 2nd Cross, Indiranagar, Bangalore 560038', ST_SetSRID(ST_MakePoint(77.6411, 12.9783), 4326), true),
('aaddb70c-1686-44a9-a33f-acd2866eb473', '19d1d9b6-6a45-4999-8585-28b3c01f5295', 'work', 'WeWork Galaxy, Residency Road, Bangalore 560025', ST_SetSRID(ST_MakePoint(77.6044, 12.9734), 4326), false),
('c2c5418a-015b-47dc-a5fc-3c7e43f91dee', 'f3068104-d3b9-48fb-a809-c12713d888ae', 'home', '15, 4th Main, Koramangala, Bangalore 560034', ST_SetSRID(ST_MakePoint(77.6200, 12.9350), 4326), true);

-- Bookings
-- 1. REQUESTED (Looking for providers)
INSERT INTO bookings (id, consumer_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('90fbbbba-1411-4e85-81dc-3fce3f7b7c73', '19d1d9b6-6a45-4999-8585-28b3c01f5295', '572cc978-e1e6-49b6-b5b1-876d1c25aef0', '2b96c23e-7b8c-4486-a3a5-dc6509cdee60', 'REQUESTED', CURRENT_TIMESTAMP + INTERVAL '1 day', '1234', 1999.0, 399.8, 1599.2, 'ONLINE', 'PENDING');

-- 2. ACCEPTED (Provider assigned, not yet started journey)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('bd48e433-5622-4d02-8caf-b7afde89b5b9', '19d1d9b6-6a45-4999-8585-28b3c01f5295', 'bbcd0d11-824a-4c72-9649-6e3a195873e2', '1e9d6da5-200f-4cdd-92cb-e1af2c08640c', 'aaddb70c-1686-44a9-a33f-acd2866eb473', 'ACCEPTED', CURRENT_TIMESTAMP + INTERVAL '2 hours', '5678', 349.0, 69.8, 279.2, 'CASH', 'PENDING');

-- 3. EN_ROUTE (Provider traveling to consumer)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('79e79e5a-580d-43dd-ad39-dd0c89c7ab1d', 'f3068104-d3b9-48fb-a809-c12713d888ae', '952e3efd-562d-4f97-9717-f5a7eb256e12', 'a5a25f09-41de-4f76-a108-2230d2c23944', 'c2c5418a-015b-47dc-a5fc-3c7e43f91dee', 'EN_ROUTE', CURRENT_TIMESTAMP, '9012', 299.0, 59.8, 239.2, 'ONLINE', 'PENDING');

-- 4. IN_PROGRESS (Provider is working)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('5e4fff50-082e-407b-b15f-833939af98b8', '19d1d9b6-6a45-4999-8585-28b3c01f5295', '1e54a653-94b6-4325-afa7-60f58c2a3486', 'ffe0462a-25b0-4aa6-8243-965572368c65', '2b96c23e-7b8c-4486-a3a5-dc6509cdee60', 'IN_PROGRESS', CURRENT_TIMESTAMP - INTERVAL '1 hour', '3456', 799.0, 159.8, 639.2, 'ONLINE', 'PENDING');

-- 5. COMPLETED (Job done, paid)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('e4233af8-1196-470c-aedd-d5c80a511b05', 'f3068104-d3b9-48fb-a809-c12713d888ae', '3de586fa-6fd9-421c-8e44-3d7076a97426', 'ac5a39d2-7f13-47f6-b5dd-d56e28584be9', 'c2c5418a-015b-47dc-a5fc-3c7e43f91dee', 'COMPLETED', CURRENT_TIMESTAMP - INTERVAL '2 days', '7890', 499.0, 99.8, 399.2, 'ONLINE', 'PAID');

-- Payment for COMPLETED booking
INSERT INTO payments (id, booking_id, razorpay_payment_id, razorpay_order_id, amount, platform_fee, provider_settlement, status, method) VALUES
(gen_random_uuid(), 'e4233af8-1196-470c-aedd-d5c80a511b05', 'pay_mock123', 'order_mock123', 499.0, 99.8, 399.2, 'CAPTURED', 'CARD');

-- Status Logs
INSERT INTO booking_status_logs (id, booking_id, from_status, to_status, changed_by, metadata) VALUES
(gen_random_uuid(), 'bd48e433-5622-4d02-8caf-b7afde89b5b9', 'REQUESTED', 'ACCEPTED', 'bbcd0d11-824a-4c72-9649-6e3a195873e2', '{"note":"accepted via broadcast"}'),

(gen_random_uuid(), 'e4233af8-1196-470c-aedd-d5c80a511b05', 'REQUESTED', 'ACCEPTED', '3de586fa-6fd9-421c-8e44-3d7076a97426', '{"note":"accepted"}'),
(gen_random_uuid(), 'e4233af8-1196-470c-aedd-d5c80a511b05', 'ACCEPTED', 'IN_PROGRESS', '3de586fa-6fd9-421c-8e44-3d7076a97426', '{"note":"started"}'),
(gen_random_uuid(), 'e4233af8-1196-470c-aedd-d5c80a511b05', 'IN_PROGRESS', 'COMPLETED', '3de586fa-6fd9-421c-8e44-3d7076a97426', '{"note":"finished"}');
