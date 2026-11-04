use serde::Deserialize;
use serde_json::json;
use url::Url;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

use super::{agents_url, fingerprint, sign, verify_claim, Api, Claim, HttpApi, Signed};
use crate::libs::error::AgentError;
use crate::libs::exports::{Export, Reported};
use crate::libs::state::Credential;

const VECTORS: &str = include_str!("../../../../core/tests/data/agent_signing_vectors.json");
const AGENT: &str = "11111111-2222-4333-8444-555555555555";

#[derive(Deserialize)]
struct Vectors {
    signing: Vec<SigningCase>,
    claim: Vec<ClaimCase>,
}

#[derive(Deserialize)]
struct SigningCase {
    method: String,
    path: String,
    body: String,
    timestamp: i64,
    key: String,
    signature: String,
}

#[derive(Deserialize)]
struct ClaimCase {
    code: String,
    fingerprint: String,
    body: String,
    signature: String,
}

fn vectors() -> Vectors {
    serde_json::from_str(VECTORS).unwrap()
}

fn credential() -> Credential {
    Credential {
        header: "k1:1:1893456000".into(),
        expires_at: 1_893_456_000,
        key: "beta".into(),
    }
}

fn exports() -> Vec<Export> {
    vec![Export {
        name: "gamma".into(),
        path: "/srv/gamma".into(),
    }]
}

fn api(server: &MockServer) -> HttpApi {
    HttpApi::new(Url::parse(&format!("{}/", server.uri())).unwrap()).unwrap()
}

fn signed_by(key: &'static str, header: &'static str) -> impl Fn(&Request) -> bool {
    move |request: &Request| {
        let value = |name: &str| {
            request
                .headers
                .get(name)
                .and_then(|value| value.to_str().ok())
                .unwrap_or_default()
                .to_owned()
        };
        let timestamp = value("X-Timestamp");
        let expected = sign(
            key,
            &timestamp,
            request.method.as_str(),
            request.url.path(),
            &request.body,
        )
        .unwrap();
        value("X-Credential") == header && value("X-Signature") == expected
    }
}

#[test]
fn signing_gives_the_answers_the_stack_holds() {
    for case in vectors().signing {
        let signature = sign(
            &case.key,
            &case.timestamp.to_string(),
            &case.method,
            &case.path,
            case.body.as_bytes(),
        )
        .unwrap();

        assert_eq!(signature, case.signature, "{} {}", case.method, case.path);
    }
}

#[test]
fn a_claim_answer_verifies_only_as_it_was_signed() {
    for case in vectors().claim {
        verify_claim(&case.code, case.body.as_bytes(), &case.signature).unwrap();

        let tampered = format!("{} ", case.body);
        assert!(verify_claim(&case.code, tampered.as_bytes(), &case.signature).is_err());
        assert!(verify_claim("delta", case.body.as_bytes(), &case.signature).is_err());
        assert!(verify_claim(&case.code, case.body.as_bytes(), "not hex").is_err());
    }
}

#[test]
fn the_fingerprint_is_the_one_the_dashboard_shows() {
    for case in vectors().claim {
        assert_eq!(fingerprint(&case.code), case.fingerprint);
    }
}

#[test]
fn every_address_sits_under_the_gateway_path() {
    let base = Url::parse("https://stack.example.com/").unwrap();

    let url = agents_url(&base, &format!("{AGENT}/socket")).unwrap();

    assert_eq!(url.path(), format!("/agent/agents/{AGENT}/socket"));
}

#[tokio::test]
async fn a_registration_carries_the_exports_and_answers_the_code() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/register"))
        .and(body_json(json!({
            "name": "alpha",
            "labels": ["delta"],
            "agent_id": AGENT,
            "exports": [{"name": "gamma", "path": "/srv/gamma"}],
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({"claim_code": "epsilon"})))
        .expect(1)
        .mount(&server)
        .await;

    let code = api(&server)
        .register("alpha", &["delta".to_owned()], Some(AGENT), &exports())
        .await
        .unwrap();

    assert_eq!(code, "epsilon");
}

#[tokio::test]
async fn a_new_agent_names_no_id() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/register"))
        .and(body_json(json!({
            "name": "alpha",
            "labels": [],
            "exports": [],
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({"claim_code": "epsilon"})))
        .expect(1)
        .mount(&server)
        .await;

    api(&server)
        .register("alpha", &[], None, &[])
        .await
        .unwrap();
}

#[tokio::test]
async fn a_rate_limited_registration_is_an_error_to_retry() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/register"))
        .respond_with(ResponseTemplate::new(429))
        .mount(&server)
        .await;

    let error = api(&server).register("alpha", &[], None, &[]).await;

    assert!(matches!(error, Err(AgentError::Api(message)) if message.contains("429")));
}

async fn claimed(template: ResponseTemplate, code: &str) -> Result<Claim, AgentError> {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/claim"))
        .and(body_json(json!({"code": code})))
        .respond_with(template)
        .mount(&server)
        .await;
    api(&server).claim(code).await
}

#[tokio::test]
async fn a_claim_waits_on_409_and_is_refused_on_401() {
    assert_eq!(
        claimed(ResponseTemplate::new(409), "delta").await.unwrap(),
        Claim::Waiting
    );
    assert_eq!(
        claimed(ResponseTemplate::new(401), "delta").await.unwrap(),
        Claim::Refused
    );
}

#[tokio::test]
async fn a_claim_on_503_or_429_is_an_error_to_retry() {
    assert!(claimed(ResponseTemplate::new(503), "delta").await.is_err());
    assert!(claimed(ResponseTemplate::new(429), "delta").await.is_err());
}

#[tokio::test]
async fn a_granted_claim_hands_over_the_credential_of_the_vector() {
    let case = vectors().claim.remove(0);
    let template = ResponseTemplate::new(200)
        .insert_header("X-Signature", case.signature.as_str())
        .set_body_string(case.body.clone());

    let claim = claimed(template, &case.code).await.unwrap();

    let Claim::Granted {
        agent_id,
        credential,
    } = claim
    else {
        panic!("not granted");
    };
    assert_eq!(agent_id, AGENT);
    assert_eq!(credential.header, "k1:1:1893456000");
    assert_eq!(credential.expires_at, 1_893_456_000);
}

#[tokio::test]
async fn a_claim_answer_with_a_wrong_or_no_signature_is_not_believed() {
    let case = vectors().claim.remove(0);
    let wrong = ResponseTemplate::new(200)
        .insert_header("X-Signature", "00")
        .set_body_string(case.body.clone());
    let none = ResponseTemplate::new(200).set_body_string(case.body.clone());

    assert!(claimed(wrong, &case.code).await.is_err());
    assert!(claimed(none, &case.code).await.is_err());
}

#[tokio::test]
async fn a_refresh_is_signed_and_answers_the_fresh_credential() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/agent/agents/{AGENT}/credential")))
        .and(signed_by("beta", "k1:1:1893456000"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "credential": "k1:2:1893459600",
            "expires_at": 1_893_459_600,
            "key": "delta",
        })))
        .expect(1)
        .mount(&server)
        .await;

    let fresh = api(&server).refresh(AGENT, &credential()).await.unwrap();

    assert_eq!(
        fresh,
        Signed::Accepted(Credential {
            header: "k1:2:1893459600".into(),
            expires_at: 1_893_459_600,
            key: "delta".into(),
        })
    );
}

#[tokio::test]
async fn only_a_401_refuses_a_signed_call() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/agent/agents/{AGENT}/credential")))
        .respond_with(ResponseTemplate::new(401))
        .mount(&server)
        .await;
    Mock::given(method("PUT"))
        .and(path(format!("/agent/agents/{AGENT}/exports")))
        .respond_with(ResponseTemplate::new(503))
        .mount(&server)
        .await;
    let api = api(&server);

    assert_eq!(
        api.refresh(AGENT, &credential()).await.unwrap(),
        Signed::Refused
    );
    assert!(api.put_exports(AGENT, &credential(), &[]).await.is_err());
}

#[tokio::test]
async fn exports_are_signed_over_the_exact_body_and_answer_their_standing() {
    let server = MockServer::start().await;
    Mock::given(method("PUT"))
        .and(path(format!("/agent/agents/{AGENT}/exports")))
        .and(signed_by("beta", "k1:1:1893456000"))
        .and(body_json(json!({
            "exports": [{"name": "gamma", "path": "/srv/gamma"}],
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "exports": [{"name": "gamma", "host_path": "/srv/gamma", "state": "pending"}],
        })))
        .expect(1)
        .mount(&server)
        .await;

    let answer = api(&server)
        .put_exports(AGENT, &credential(), &exports())
        .await
        .unwrap();

    assert_eq!(
        answer,
        Signed::Accepted(vec![Reported {
            name: "gamma".into(),
            host_path: "/srv/gamma".into(),
            state: "pending".into(),
            bound_to: None,
        }])
    );
}

#[tokio::test]
async fn a_body_that_makes_no_sense_is_not_quoted() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/agent/agents/{AGENT}/credential")))
        .respond_with(ResponseTemplate::new(200).set_body_string(r#"{"key": "delta"}"#))
        .mount(&server)
        .await;

    let error = api(&server)
        .refresh(AGENT, &credential())
        .await
        .unwrap_err()
        .to_string();

    assert!(!error.contains("delta"));
}

#[tokio::test]
async fn a_redirect_is_not_followed() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/claim"))
        .respond_with(
            ResponseTemplate::new(307).insert_header("Location", "https://other.example.com/"),
        )
        .expect(1)
        .mount(&server)
        .await;

    assert!(api(&server).claim("delta").await.is_err());
}
