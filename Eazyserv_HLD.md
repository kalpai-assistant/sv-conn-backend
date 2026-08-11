# High-Level Design — On-Demand Service Marketplace

**Project Codename:** Eazyserv  
**Document Version:** 1.0  
**Date:** April 14, 2026  
**Author:** Vipul Rao  
**Status:** Draft

---

## 1. Executive Summary

Eazyserv is a B2C on-demand home services marketplace — similar in model to Urban Company (India) or TaskRabbit (US) — with an Uber/Ola-style live tracking experience. The platform connects service providers (plumbers, electricians, cleaners, etc.) with consumers via a mobile application.

**Revenue Model:** 20% platform commission on every completed booking. On a ₹100 service, ₹20 goes to the platform and ₹80 is settled to the service provider.

**Tech Stack:**
- Backend: Python (FastAPI)
- Mobile: React Native (Expo)
- Admin Panel: React.js (Web)
- Database: PostgreSQL + PostGIS
- Cache/Queue: Redis
- Payments: Razorpay (Route API for splits)

---

## 2. System Overview

### 2.1 User Roles

| Role | Description |
|------|-------------|
| Consumer | End user who browses, books, and pays for services |
| Service Provider | Professional who lists services, accepts bookings, and fulfils them |
| Super Admin | Platform operator managing providers, analytics, disputes, and payouts |

### 2.2 Core User Journeys

**Consumer Journey:**
1. Register/login via phone OTP
2. Set current location (GPS or manual address)
3. Browse service categories → view services → select a provider
4. Book appointment (instant or scheduled)
5. Track provider live on map (en route)
6. Share OTP at doorstep for verification
7. Service completed → pay (cash or online) → rate/review → invoice

**Provider Journey:**
1. Register with phone, Aadhaar/PAN, bank details (UPI), address
2. List services under categories with pricing
3. Receive booking notifications → accept/reject
4. Navigate to consumer location
5. Enter OTP at doorstep to start service
6. Mark service as completed
7. Receive 80% payout via Razorpay Route (T+2 settlement)

---

## 3. Architecture

### 3.1 Architecture Pattern

**Modular monolith** — a single FastAPI application with well-separated domain modules (routers, services, models). This avoids premature microservice complexity while keeping the codebase ready for future extraction.

```
Client Layer
├── Consumer App (React Native / Expo)
├── Provider App (React Native / Expo)
└── Admin Panel (React.js / Web)
        │
        ▼
API Gateway (Nginx / Traefik)
├── SSL termination (Let's Encrypt)
├── Rate limiting
├── JWT validation pass-through
        │
        ▼
FastAPI Backend (Monolith)
├── /api/v1/auth        → Auth + OTP module
├── /api/v1/users       → User profiles module
├── /api/v1/services    → Service catalog module
├── /api/v1/bookings    → Booking engine module
├── /api/v1/payments    → Payments module
├── /api/v1/tracking    → Live location (WebSocket)
├── /api/v1/reviews     → Ratings & reviews module
├── /api/v1/notifications → Notification dispatch
└── /api/v1/admin       → Admin analytics & management
        │
        ▼
Data Layer
├── PostgreSQL + PostGIS  → Primary datastore
├── Redis                 → Caching, sessions, Celery broker, live location pub/sub
└── S3 / MinIO            → Profile images, documents, invoices
        │
        ▼
External Services
├── Razorpay              → Payments + Route API (split settlements)
├── MSG91 / Twilio        → SMS OTP
├── Firebase Cloud Messaging → Push notifications
└── Google Maps Platform  → Geocoding, directions, distance matrix
```

### 3.2 Communication Patterns

| Pattern | Use Case | Implementation |
|---------|----------|----------------|
| REST (HTTP) | All CRUD operations, booking lifecycle | FastAPI routers |
| WebSocket | Live provider location tracking | FastAPI WebSocket endpoint + Redis pub/sub |
| Background Tasks | Notifications, invoice generation, settlement reconciliation | Celery + Redis as broker |
| Push Notifications | Booking alerts, status updates, promotions | Firebase Cloud Messaging (FCM) |

---

## 4. Module Design

### 4.1 Auth + OTP Module

**Responsibilities:** User registration, phone-based OTP login, JWT token management, role-based access control.

**Flow:**
1. User submits phone number
2. Backend generates 6-digit OTP → sends via MSG91/Twilio
3. User submits OTP → backend verifies → issues JWT access + refresh tokens
4. JWT payload: `{ user_id, role (consumer|provider|admin), exp }`

**Key Decisions:**
- Phone-only auth (no email/password) — matches Indian market UX expectations
- OTP rate limiting: max 5 requests per phone per hour
- JWT access token TTL: 15 minutes; refresh token: 30 days
- Doorstep OTP is a separate 4-digit code generated per booking, not reused

### 4.2 User Profiles Module

**Consumer Profile:**
- Phone number (unique, verified)
- Name, email (optional)
- Saved addresses (home, work, custom) with lat/long
- Current GPS location

**Provider Profile:**
- Phone number (unique, verified)
- Name, profile photo
- Aadhaar/PAN (KYC — stored encrypted, verified manually or via DigiLocker API)
- Bank account or UPI ID for settlements
- Service area (lat/long center + radius)
- Average rating, total completed jobs
- Active/inactive status

### 4.3 Service Catalog Module

**Data Model:**
- Categories (e.g., Home Cleaning, Plumbing, Electrical, Salon)
- Services nested under categories (e.g., "Deep Cleaning — 2BHK" under Home Cleaning)
- Each service has: name, description, base price, estimated duration, provider ID
- Providers can list multiple services across categories

**Search & Discovery:**
- Filter by category, price range, rating
- Location-based: show only providers whose service area covers the consumer's location (PostGIS `ST_DWithin` query)
- Sort by: distance, rating, price

### 4.4 Booking Engine Module

**Booking Lifecycle (State Machine):**

```
 ┌──────────┐     ┌──────────┐     ┌─────────────┐     ┌─────────────┐     ┌───────────┐
 │ REQUESTED│────▶│ ACCEPTED │────▶│ EN_ROUTE    │────▶│ IN_PROGRESS │────▶│ COMPLETED │
 └──────────┘     └──────────┘     └─────────────┘     └─────────────┘     └───────────┘
       │                │                                                        │
       ▼                ▼                                                        ▼
 ┌──────────┐     ┌──────────┐                                            ┌───────────┐
 │ EXPIRED  │     │ CANCELLED│                                            │ PAYMENT   │
 └──────────┘     └──────────┘                                            └───────────┘
```

**State Transitions:**
| From | To | Trigger |
|------|----|---------|
| REQUESTED | ACCEPTED | Provider accepts within 2-minute window |
| REQUESTED | EXPIRED | No provider accepts within timeout |
| ACCEPTED | EN_ROUTE | Provider starts navigation |
| ACCEPTED | CANCELLED | Consumer or provider cancels (cancellation fee logic applies) |
| EN_ROUTE | IN_PROGRESS | Provider enters doorstep OTP → verified |
| IN_PROGRESS | COMPLETED | Provider marks job as done |
| COMPLETED | PAYMENT | Automatic transition → triggers payment flow |

**Key Rules:**
- Every state change is logged in `booking_status_log` for audit
- Consumer receives push notification on every state change
- Cancellation after acceptance incurs ₹50 flat fee (configurable)
- Auto-expire REQUESTED bookings after 2 minutes if no provider accepts

### 4.5 Live Tracking Module

**Architecture:**
```
Provider App                    Server                        Consumer App
    │                             │                               │
    │──── WS connect ────────────▶│                               │
    │──── location update ───────▶│──── Redis pub/sub ───────────▶│
    │     (every 5 sec)           │     channel: booking:{id}     │
    │                             │──── WS push to consumer ─────▶│
    │                             │                               │
```

**Implementation Notes:**
- Provider app sends GPS coordinates every 5 seconds via WebSocket while EN_ROUTE
- Server publishes to Redis channel `tracking:{booking_id}`
- Consumer app subscribes to the same channel and renders on Google Maps
- Background location on Android: use `expo-location` with `startLocationUpdatesAsync` (foreground service notification required for Android 10+)
- Battery optimization: reduce frequency to 10 seconds after 5 minutes, stop on arrival

**MVP Alternative:** Skip continuous GPS streaming. Instead, send provider location only on state changes (accepted → en route → arrived). Show ETA using Google Maps Distance Matrix API. Add live tracking in v2.

### 4.6 Payments Module

**Payment Flows:**

**Online Payment (Razorpay):**
1. On booking completion, backend creates a Razorpay Order with `transfers` array
2. Razorpay Route API auto-splits: 80% to provider's linked account, 20% retained
3. Consumer pays via UPI / card / net banking through Razorpay checkout in-app
4. Webhook confirms payment → booking status moves to PAID
5. Invoice generated (PDF) and sent via push + email

**Cash Payment:**
1. Consumer pays provider directly
2. Provider confirms cash collected in-app
3. Platform commission (20%) is deducted from provider's next online payout (running balance model)
4. If provider's balance goes negative, flag for manual settlement

**Razorpay Route Setup:**
- Each provider is onboarded as a "linked account" via Razorpay Route
- KYC: Aadhaar + PAN + bank account verification
- Settlement cycle: T+2 business days
- Minimum payout: ₹100

### 4.7 Notifications Module

**Channels (Priority Order):**

| Channel | Use Case | Provider |
|---------|----------|----------|
| Push (FCM) | All booking status updates, new booking requests | Firebase Cloud Messaging |
| SMS | OTP, critical booking alerts (fallback if push fails) | MSG91 |
| WhatsApp | Booking confirmation, invoice delivery (Phase 2) | WhatsApp Business API |
| Email | Invoices, weekly provider earnings summary (Phase 2) | SMTP / SES |

**Implementation:**
- Celery task queue for async notification dispatch
- Notification preferences stored per user (opt-in/out per channel)
- Retry logic: 3 attempts with exponential backoff

### 4.8 Reviews & Ratings Module

- Consumers rate providers (1-5 stars + optional text) after booking completion
- Providers can rate consumers (affects consumer trust score)
- Average rating recalculated on each new review (weighted moving average)
- Reviews visible on provider profile
- Admin can moderate/remove inappropriate reviews

### 4.9 Admin Panel Module

**Features:**
- Dashboard: total bookings, revenue, active providers, new users (daily/weekly/monthly)
- Provider management: KYC approval, activate/deactivate, view earnings
- Booking management: view all bookings, handle disputes, issue refunds
- Service catalog management: add/edit categories and services
- Commission rules: configure platform percentage (default 20%)
- Reports: CSV/PDF export of transactions, settlements, provider payouts

**Tech:** React.js SPA consuming the same FastAPI backend via `/api/v1/admin` routes with admin-only JWT middleware.

---

## 5. Database Design

### 5.1 Core Tables

```
users
├── id (UUID, PK)
├── phone (VARCHAR, UNIQUE)
├── name (VARCHAR)
├── email (VARCHAR, nullable)
├── role (ENUM: consumer, provider, admin)
├── is_active (BOOLEAN)
├── created_at (TIMESTAMP)
└── updated_at (TIMESTAMP)

provider_profiles
├── id (UUID, PK)
├── user_id (FK → users)
├── aadhaar_number (VARCHAR, encrypted)
├── pan_number (VARCHAR, encrypted)
├── bank_account / upi_id (VARCHAR, encrypted)
├── profile_photo_url (VARCHAR)
├── service_area_center (GEOGRAPHY POINT)
├── service_area_radius_km (INTEGER)
├── avg_rating (DECIMAL)
├── total_completed_jobs (INTEGER)
├── razorpay_linked_account_id (VARCHAR)
├── kyc_status (ENUM: pending, verified, rejected)
└── is_online (BOOLEAN)

addresses
├── id (UUID, PK)
├── user_id (FK → users)
├── label (VARCHAR: home, work, other)
├── full_address (TEXT)
├── location (GEOGRAPHY POINT)
└── is_default (BOOLEAN)

categories
├── id (UUID, PK)
├── name (VARCHAR)
├── icon_url (VARCHAR)
├── sort_order (INTEGER)
└── is_active (BOOLEAN)

services
├── id (UUID, PK)
├── category_id (FK → categories)
├── provider_id (FK → users)
├── name (VARCHAR)
├── description (TEXT)
├── base_price (DECIMAL)
├── estimated_duration_minutes (INTEGER)
└── is_active (BOOLEAN)

bookings
├── id (UUID, PK)
├── consumer_id (FK → users)
├── provider_id (FK → users)
├── service_id (FK → services)
├── status (ENUM: requested, accepted, en_route,
│          in_progress, completed, cancelled, expired)
├── consumer_address_id (FK → addresses)
├── scheduled_at (TIMESTAMP, nullable for instant bookings)
├── doorstep_otp (VARCHAR, 4 digits)
├── service_amount (DECIMAL)
├── platform_commission (DECIMAL)
├── provider_payout (DECIMAL)
├── payment_method (ENUM: cash, online)
├── payment_status (ENUM: pending, paid, refunded)
├── razorpay_order_id (VARCHAR, nullable)
├── created_at (TIMESTAMP)
└── updated_at (TIMESTAMP)

booking_status_log
├── id (UUID, PK)
├── booking_id (FK → bookings)
├── from_status (VARCHAR)
├── to_status (VARCHAR)
├── changed_by (FK → users)
├── metadata (JSONB)
└── created_at (TIMESTAMP)

reviews
├── id (UUID, PK)
├── booking_id (FK → bookings)
├── reviewer_id (FK → users)
├── reviewee_id (FK → users)
├── rating (INTEGER, 1-5)
├── comment (TEXT, nullable)
└── created_at (TIMESTAMP)

payments
├── id (UUID, PK)
├── booking_id (FK → bookings)
├── razorpay_payment_id (VARCHAR)
├── razorpay_order_id (VARCHAR)
├── amount (DECIMAL)
├── platform_fee (DECIMAL)
├── provider_settlement (DECIMAL)
├── status (ENUM: created, captured, failed, refunded)
├── method (ENUM: upi, card, netbanking, cash)
└── created_at (TIMESTAMP)
```

### 5.2 Key Indexes

```sql
-- Location-based provider search (PostGIS)
CREATE INDEX idx_provider_service_area
  ON provider_profiles USING GIST (service_area_center);

-- Active bookings for a consumer/provider
CREATE INDEX idx_bookings_consumer_status
  ON bookings (consumer_id, status) WHERE status NOT IN ('completed', 'cancelled', 'expired');

CREATE INDEX idx_bookings_provider_status
  ON bookings (provider_id, status) WHERE status NOT IN ('completed', 'cancelled', 'expired');

-- Services by category (catalog browsing)
CREATE INDEX idx_services_category_active
  ON services (category_id) WHERE is_active = true;
```

---

## 6. API Design

### 6.1 Key Endpoints

**Auth:**
```
POST   /api/v1/auth/send-otp          { phone }
POST   /api/v1/auth/verify-otp        { phone, otp }
POST   /api/v1/auth/refresh            { refresh_token }
```

**Users:**
```
GET    /api/v1/users/me
PUT    /api/v1/users/me                { name, email }
POST   /api/v1/users/me/addresses      { label, full_address, lat, lng }
```

**Provider Profile:**
```
POST   /api/v1/providers/register       { aadhaar, pan, bank_details }
PUT    /api/v1/providers/me/status       { is_online: true/false }
PUT    /api/v1/providers/me/location     { lat, lng }
```

**Service Catalog:**
```
GET    /api/v1/categories
GET    /api/v1/services?category_id=X&lat=Y&lng=Z&radius=10
POST   /api/v1/services                 { category_id, name, price, duration } (provider)
```

**Bookings:**
```
POST   /api/v1/bookings                 { service_id, address_id, scheduled_at? }
PATCH  /api/v1/bookings/{id}/accept     (provider)
PATCH  /api/v1/bookings/{id}/start      { otp } (provider — doorstep verification)
PATCH  /api/v1/bookings/{id}/complete   (provider)
PATCH  /api/v1/bookings/{id}/cancel     { reason }
GET    /api/v1/bookings/{id}
GET    /api/v1/bookings?status=active
```

**Payments:**
```
POST   /api/v1/payments/create-order    { booking_id }
POST   /api/v1/payments/verify          { razorpay_payment_id, razorpay_signature }
POST   /api/v1/payments/webhook         (Razorpay webhook — server-to-server)
```

**Tracking (WebSocket):**
```
WS     /api/v1/tracking/{booking_id}    (consumer subscribes)
WS     /api/v1/tracking/provider        (provider publishes location)
```

**Reviews:**
```
POST   /api/v1/reviews                  { booking_id, rating, comment }
GET    /api/v1/reviews?provider_id=X
```

---

## 7. Infrastructure & Deployment

### 7.1 Infrastructure (₹7K/month Budget)

| Component | Service | Estimated Cost |
|-----------|---------|---------------|
| App Server | AWS EC2 t3.small (2 vCPU, 2GB RAM) | ₹1,500/mo |
| Database | RDS PostgreSQL db.t3.micro (1 vCPU, 1GB) | ₹2,000/mo |
| Cache/Queue | ElastiCache Redis t3.micro | ₹1,500/mo |
| Storage | S3 (10GB) | ₹50/mo |
| Domain | GoDaddy (.in domain) | ₹500/yr |
| SSL | Let's Encrypt | Free |
| SMS OTP | MSG91 (~500 OTPs/mo at ₹0.20 each) | ₹100/mo |
| Push Notifications | Firebase Cloud Messaging | Free |
| Maps API | Google Maps (₹16,000 free credit/mo) | Free (under limit) |
| **Total** | | **~₹5,650/mo** |

### 7.2 Deployment Architecture

```
                    ┌─────────────────┐
                    │  CloudFlare CDN │
                    │  (SSL + Cache)  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  EC2 (t3.small) │
                    │                 │
                    │  Nginx (reverse │
                    │   proxy + SSL)  │
                    │       │         │
                    │  FastAPI (uvicorn│
                    │   + gunicorn)   │
                    │       │         │
                    │  Celery worker  │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ RDS      │  │ Redis    │  │ S3       │
        │ Postgres │  │ ElastiC. │  │ Bucket   │
        └──────────┘  └──────────┘  └──────────┘
```

### 7.3 CI/CD

- GitHub Actions for automated testing + deployment
- Docker containers for reproducible builds
- Deployment: `docker compose` on EC2 (single-server setup for MVP)
- Database migrations: Alembic

---

## 8. Security Considerations

| Area | Approach |
|------|----------|
| Authentication | Phone OTP + JWT (access + refresh tokens) |
| Authorization | Role-based middleware (consumer, provider, admin) |
| Data Encryption | AES-256 for PII (Aadhaar, PAN, bank details) at rest |
| Transport | HTTPS everywhere (TLS 1.3 via CloudFlare/Let's Encrypt) |
| API Security | Rate limiting (Nginx), input validation (Pydantic), CORS whitelist |
| Payment Security | Razorpay handles PCI compliance; server-side signature verification |
| OTP Security | Rate limit 5 OTPs/hour/phone, 10-minute expiry, bcrypt hash before storage |
| Provider KYC | Aadhaar + PAN verification (manual initially, DigiLocker API later) |
| Injection Prevention | Parameterized queries via SQLAlchemy ORM, no raw SQL |

---

## 9. Scalability Path

**Phase 1 (0-1K bookings/month — current design):**
- Single EC2 + RDS + Redis
- Monolith FastAPI
- Sufficient for MVP and early traction

**Phase 2 (1K-10K bookings/month):**
- Upgrade to t3.medium (4GB RAM)
- Add read replica for PostgreSQL
- Introduce connection pooling (PgBouncer)
- Add APM monitoring (Sentry + Grafana)

**Phase 3 (10K+ bookings/month):**
- Extract booking engine and tracking into separate services
- Add load balancer (ALB) with multiple app servers
- Move to managed Kubernetes (EKS) or ECS
- Dedicated WebSocket server for tracking
- Elasticsearch for service search

---

## 10. MVP Roadmap

| Phase | Scope | Timeline |
|-------|-------|----------|
| **Phase 1** | Auth + OTP, consumer/provider profiles, service catalog, basic search | Weeks 1-3 |
| **Phase 2** | Booking engine (full state machine), push notifications | Weeks 4-5 |
| **Phase 3** | Razorpay integration, payment splits, invoice generation | Weeks 6-7 |
| **Phase 4** | Reviews, admin panel (basic), QA + polish | Week 8 |
| **Phase 5** | Live tracking, WhatsApp notifications, scheduled bookings | Weeks 9-10 |

### 10.1 Deferred to v2

- Live GPS tracking (use status-based updates in v1)
- Multi-language support
- In-app chat between consumer and provider
- Advanced analytics dashboard (use Metabase in v1)
- Referral/coupon system
- Provider subscription tiers
- Multi-city expansion tooling

---

## 11. Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| React Native learning curve | 3-4 week ramp-up delay | Use Expo managed workflow to minimize native config |
| Provider supply shortage | No bookings without providers | Onboard 20-30 providers manually before consumer launch |
| Cash collection leakage | Providers underreport cash bookings | Incentivize online payments (lower commission for online) |
| Background location battery drain | Poor provider app reviews | Reduce GPS frequency, use significant-change monitoring |
| Razorpay Route KYC delays | Provider onboarding bottleneck | Start KYC process during provider registration, not first booking |
| Single server failure | Full outage | Daily automated RDS snapshots, EC2 AMI backups |

---

## 12. Cost Summary (Monthly Recurring)

| Category | Cost |
|----------|------|
| AWS Infrastructure | ₹5,150 |
| Domain + Email | ₹100 |
| SMS (OTP) | ₹100-300 |
| Google Maps API | Free (under free tier) |
| Firebase FCM | Free |
| Razorpay | 2% per online transaction (no fixed fee) |
| **Total Fixed** | **~₹5,650/mo** |
| **Budget Remaining** | **~₹1,350/mo buffer** |

---

*This document is a living artifact. Update as architecture decisions evolve during implementation.*
