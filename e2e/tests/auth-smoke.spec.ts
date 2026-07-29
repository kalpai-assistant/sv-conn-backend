/**
 * Nightly auth smoke (SC-52).
 *
 * Exercises the send-OTP → verify-OTP → token-issued path against staging,
 * using the E2E bypass on the backend so no SMS provider is in the loop.
 *
 * If this test fails: either staging is down, the deploy is broken, or the
 * bypass has been misconfigured on staging (E2E_TEST_PHONES / E2E_TEST_OTP).
 * The email from GH Actions carries the failing step + URL.
 */
import { test, expect } from "@playwright/test";

const phone = process.env.E2E_TEST_PHONE;
const otp = process.env.E2E_TEST_OTP;

test.beforeAll(() => {
  // Fail loudly if the env isn't wired — better than a silent 400 in the test.
  if (!phone) throw new Error("E2E_TEST_PHONE not set");
  if (!otp) throw new Error("E2E_TEST_OTP not set");
});

test("auth smoke: send-otp → verify-otp → token issued", async ({ request }) => {
  // Some rate-limit slack from previous runs. Idempotent — safe to repeat.
  const sendRes = await request.post("/api/v1/auth/send-otp", {
    data: { phone },
  });
  expect(sendRes.status(), await sendRes.text()).toBe(200);

  const verifyRes = await request.post("/api/v1/auth/verify-otp", {
    data: { phone, otp, role: "consumer" },
  });
  expect(verifyRes.status(), await verifyRes.text()).toBe(200);

  const body = await verifyRes.json();
  expect(body.access_token, "access_token missing").toBeTruthy();
  expect(body.refresh_token, "refresh_token missing").toBeTruthy();
  expect(body.token_type).toBe("bearer");
  expect(body.role).toBe("consumer");
  expect(typeof body.user_id).toBe("string");
});

test("liveness probe returns 200", async ({ request }) => {
  // Trivial but pins /health working before we chase real bugs.
  const res = await request.get("/health");
  expect(res.status()).toBe(200);
  expect(await res.json()).toEqual({ status: "ok" });
});

test("readiness probe reports database + redis ok", async ({ request }) => {
  // /healthz — real dep pings. If either fails, LB drains this pod; we want
  // the nightly to catch that state independently of the LB's own decision.
  const res = await request.get("/healthz");
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.status).toBe("ok");
  expect(body.checks.database).toBe("ok");
  expect(body.checks.redis).toBe("ok");
});
