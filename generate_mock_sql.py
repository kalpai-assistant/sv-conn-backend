import uuid

# Generate UUIDs
admin_id = str(uuid.uuid4())
consumer_ids = [str(uuid.uuid4()) for _ in range(3)]
provider_ids = [str(uuid.uuid4()) for _ in range(5)]
provider_profile_ids = [str(uuid.uuid4()) for _ in range(5)]

category_ids = [str(uuid.uuid4()) for _ in range(4)]
service_ids = [str(uuid.uuid4()) for _ in range(12)]

address_ids = [str(uuid.uuid4()) for _ in range(3)]
booking_ids = [str(uuid.uuid4()) for _ in range(5)]

sql = f"""-- Seed Mock Data for Eazyserv

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
('{admin_id}', '9999999999', 'Admin User', 'admin@svcconnect.com', 'ADMIN', true),
('{consumer_ids[0]}', '9876543200', 'Arjun Mehta', 'arjun@gmail.com', 'CONSUMER', true),
('{consumer_ids[1]}', '9876543201', 'Priya Singh', 'priya@gmail.com', 'CONSUMER', true),
('{consumer_ids[2]}', '9876543202', 'Rahul Dev', 'rahul@gmail.com', 'CONSUMER', true),
('{provider_ids[0]}', '9876543210', 'Rajesh Kumar', 'rajesh@svcconnect.com', 'PROVIDER', true),
('{provider_ids[1]}', '9876543211', 'Anita Sharma', 'anita@svcconnect.com', 'PROVIDER', true),
('{provider_ids[2]}', '9876543212', 'Mohammed Irfan', 'mohammed@svcconnect.com', 'PROVIDER', true),
('{provider_ids[3]}', '9876543213', 'Lakshmi Devi', 'lakshmi@svcconnect.com', 'PROVIDER', true),
('{provider_ids[4]}', '9876543214', 'Deepa Nair', 'deepa@svcconnect.com', 'PROVIDER', true);

-- Provider Profiles (with PostGIS location in Bangalore)
INSERT INTO provider_profiles (id, user_id, service_area_radius_km, avg_rating, total_completed_jobs, kyc_status, is_online, service_area_center) VALUES
('{provider_profile_ids[0]}', '{provider_ids[0]}', 15, 4.8, 234, 'VERIFIED', true, ST_SetSRID(ST_MakePoint(77.5946, 12.9716), 4326)),
('{provider_profile_ids[1]}', '{provider_ids[1]}', 10, 4.9, 189, 'VERIFIED', true, ST_SetSRID(ST_MakePoint(77.6412, 12.9784), 4326)),
('{provider_profile_ids[2]}', '{provider_ids[2]}', 20, 4.6, 156, 'VERIFIED', true, ST_SetSRID(ST_MakePoint(77.6101, 12.9250), 4326)),
('{provider_profile_ids[3]}', '{provider_ids[3]}', 12, 4.7, 312, 'VERIFIED', false, ST_SetSRID(ST_MakePoint(77.5505, 13.0068), 4326)),
('{provider_profile_ids[4]}', '{provider_ids[4]}', 8, 4.5, 98, 'PENDING', true, ST_SetSRID(ST_MakePoint(77.6200, 12.9350), 4326));

-- Categories
INSERT INTO categories (id, name, slug, icon_url, sort_order, is_active) VALUES
('{category_ids[0]}', 'Cleaning', 'cleaning', 'Sparkles', 1, true),
('{category_ids[1]}', 'Plumbing', 'plumbing', 'Wrench', 2, true),
('{category_ids[2]}', 'Electrical', 'electrical', 'Zap', 3, true),
('{category_ids[3]}', 'Salon', 'salon', 'Scissors', 4, true);

-- Services
INSERT INTO services (id, category_id, name, description, base_price, duration_minutes, is_active) VALUES
('{service_ids[0]}', '{category_ids[0]}', 'Full Home Deep Cleaning', 'Professional deep cleaning of entire home.', 1999.0, 180, true),
('{service_ids[1]}', '{category_ids[0]}', 'Bathroom Deep Cleaning', 'Thorough cleaning of bathrooms including tiles.', 599.0, 60, true),
('{service_ids[2]}', '{category_ids[0]}', 'Sofa Cleaning', 'Dry vacuuming and wet shampooing of sofa.', 799.0, 90, true),
('{service_ids[3]}', '{category_ids[1]}', 'Pipe Leak Repair', 'Fix leaking pipes, joints and faucets.', 349.0, 45, true),
('{service_ids[4]}', '{category_ids[1]}', 'Tap & Mixer Installation', 'Installation of new taps and mixers.', 249.0, 30, true),
('{service_ids[5]}', '{category_ids[1]}', 'Water Tank Cleaning', 'Mechanized cleaning of overhead water tanks.', 999.0, 120, true),
('{service_ids[6]}', '{category_ids[2]}', 'Fan Installation', 'Ceiling or wall fan installation with wiring.', 299.0, 45, true),
('{service_ids[7]}', '{category_ids[2]}', 'Switchboard Repair', 'Repair or replacement of switchboards.', 199.0, 30, true),
('{service_ids[8]}', '{category_ids[2]}', 'Inverter Installation', 'Setup and connection of home inverter.', 599.0, 60, true),
('{service_ids[9]}', '{category_ids[3]}', 'Womens Haircut', 'Professional haircut at your doorstep.', 499.0, 45, true),
('{service_ids[10]}', '{category_ids[3]}', 'Bridal Makeup', 'Complete bridal makeup package with HD finish.', 4999.0, 180, true),
('{service_ids[11]}', '{category_ids[3]}', 'Manicure & Pedicure', 'Relaxing mani-pedi spa treatment.', 899.0, 90, true);

-- Provider Services (Subscriptions)
INSERT INTO provider_services (id, provider_id, service_id) VALUES
(gen_random_uuid(), '{provider_ids[0]}', '{service_ids[0]}'),
(gen_random_uuid(), '{provider_ids[0]}', '{service_ids[1]}'),
(gen_random_uuid(), '{provider_ids[0]}', '{service_ids[2]}'),
(gen_random_uuid(), '{provider_ids[1]}', '{service_ids[3]}'),
(gen_random_uuid(), '{provider_ids[1]}', '{service_ids[4]}'),
(gen_random_uuid(), '{provider_ids[2]}', '{service_ids[6]}'),
(gen_random_uuid(), '{provider_ids[2]}', '{service_ids[7]}'),
(gen_random_uuid(), '{provider_ids[3]}', '{service_ids[9]}'),
(gen_random_uuid(), '{provider_ids[3]}', '{service_ids[10]}'),
(gen_random_uuid(), '{provider_ids[4]}', '{service_ids[1]}'),
(gen_random_uuid(), '{provider_ids[4]}', '{service_ids[5]}');

-- Consumer Addresses (Bangalore locations)
INSERT INTO addresses (id, user_id, label, full_address, location, is_default) VALUES
('{address_ids[0]}', '{consumer_ids[0]}', 'home', '42, 2nd Cross, Indiranagar, Bangalore 560038', ST_SetSRID(ST_MakePoint(77.6411, 12.9783), 4326), true),
('{address_ids[1]}', '{consumer_ids[0]}', 'work', 'WeWork Galaxy, Residency Road, Bangalore 560025', ST_SetSRID(ST_MakePoint(77.6044, 12.9734), 4326), false),
('{address_ids[2]}', '{consumer_ids[1]}', 'home', '15, 4th Main, Koramangala, Bangalore 560034', ST_SetSRID(ST_MakePoint(77.6200, 12.9350), 4326), true);

-- Bookings
-- 1. REQUESTED (Looking for providers)
INSERT INTO bookings (id, consumer_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('{booking_ids[0]}', '{consumer_ids[0]}', '{service_ids[0]}', '{address_ids[0]}', 'REQUESTED', CURRENT_TIMESTAMP + INTERVAL '1 day', '1234', 1999.0, 399.8, 1599.2, 'ONLINE', 'PENDING');

-- 2. ACCEPTED (Provider assigned, not yet started journey)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('{booking_ids[1]}', '{consumer_ids[0]}', '{provider_ids[1]}', '{service_ids[3]}', '{address_ids[1]}', 'ACCEPTED', CURRENT_TIMESTAMP + INTERVAL '2 hours', '5678', 349.0, 69.8, 279.2, 'CASH', 'PENDING');

-- 3. EN_ROUTE (Provider traveling to consumer)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('{booking_ids[2]}', '{consumer_ids[1]}', '{provider_ids[2]}', '{service_ids[6]}', '{address_ids[2]}', 'EN_ROUTE', CURRENT_TIMESTAMP, '9012', 299.0, 59.8, 239.2, 'ONLINE', 'PENDING');

-- 4. IN_PROGRESS (Provider is working)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('{booking_ids[3]}', '{consumer_ids[0]}', '{provider_ids[0]}', '{service_ids[2]}', '{address_ids[0]}', 'IN_PROGRESS', CURRENT_TIMESTAMP - INTERVAL '1 hour', '3456', 799.0, 159.8, 639.2, 'ONLINE', 'PENDING');

-- 5. COMPLETED (Job done, paid)
INSERT INTO bookings (id, consumer_id, provider_id, service_id, consumer_address_id, status, scheduled_at, doorstep_otp, service_amount, platform_commission, provider_payout, payment_method, payment_status) VALUES
('{booking_ids[4]}', '{consumer_ids[1]}', '{provider_ids[3]}', '{service_ids[9]}', '{address_ids[2]}', 'COMPLETED', CURRENT_TIMESTAMP - INTERVAL '2 days', '7890', 499.0, 99.8, 399.2, 'ONLINE', 'PAID');

-- Payment for COMPLETED booking
INSERT INTO payments (id, booking_id, razorpay_payment_id, razorpay_order_id, amount, platform_fee, provider_settlement, status, method) VALUES
(gen_random_uuid(), '{booking_ids[4]}', 'pay_mock123', 'order_mock123', 499.0, 99.8, 399.2, 'CAPTURED', 'CARD');

-- Status Logs
INSERT INTO booking_status_logs (id, booking_id, from_status, to_status, changed_by, metadata) VALUES
(gen_random_uuid(), '{booking_ids[1]}', 'REQUESTED', 'ACCEPTED', '{provider_ids[1]}', '{{"note":"accepted via broadcast"}}'),

(gen_random_uuid(), '{booking_ids[4]}', 'REQUESTED', 'ACCEPTED', '{provider_ids[3]}', '{{"note":"accepted"}}'),
(gen_random_uuid(), '{booking_ids[4]}', 'ACCEPTED', 'IN_PROGRESS', '{provider_ids[3]}', '{{"note":"started"}}'),
(gen_random_uuid(), '{booking_ids[4]}', 'IN_PROGRESS', 'COMPLETED', '{provider_ids[3]}', '{{"note":"finished"}}');
"""

with open("mock_data.sql", "w") as f:
    f.write(sql)
print("SQL file generated successfully.")
