# alpha

A small shop backend used as a retrieval fixture.

## Authentication

`POST /login` reaches `AuthController.login`, which asks `AuthService` to check
the password with `PasswordHasher` and signs a token with `JwtProvider`.
`requireAuth` verifies that token on protected routes. OAuth logins arrive at
`handleOAuthCallback`.

## Payments

Refunds come in through `RefundController` or the nightly `RefundJob`; both
call `PaymentService.refund`, which retries through `RetryPolicy` and emails a
receipt with `Mailer`.

## Worker

The Python worker builds daily revenue reports and purges expired sessions.
