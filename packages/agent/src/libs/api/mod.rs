use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, KeyInit, Mac};
use reqwest::redirect::Policy;
use reqwest::{Client, Method, Response, StatusCode};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use url::Url;

use crate::libs::error::AgentError;
use crate::libs::exports::{Export, Reported};
use crate::libs::import::Listed;
use crate::libs::state::Credential;

type HmacSha256 = Hmac<Sha256>;

const TIMEOUT: Duration = Duration::from_secs(10);
const FINGERPRINT_CHARS: usize = 8;

#[derive(Debug, PartialEq, Eq)]
pub enum Claim {
    Waiting,
    Refused,
    Granted {
        agent_id: String,
        credential: Credential,
    },
}

/// A signed call goes through, or the stack no longer accepts the credential.
/// Only this refusal drops a credential: a 503 or a 429 is an error to retry.
#[derive(Debug, PartialEq, Eq)]
pub enum Signed<T> {
    Accepted(T),
    Refused,
}

pub(crate) trait Api {
    async fn register(
        &self,
        name: &str,
        labels: &[String],
        agent_id: Option<&str>,
        exports: &[Export],
    ) -> Result<String, AgentError>;
    async fn claim(&self, code: &str) -> Result<Claim, AgentError>;
    async fn refresh(
        &self,
        agent_id: &str,
        credential: &Credential,
    ) -> Result<Signed<Credential>, AgentError>;
    async fn put_exports(
        &self,
        agent_id: &str,
        credential: &Credential,
        exports: &[Export],
    ) -> Result<Signed<Vec<Reported>>, AgentError>;
}

#[derive(Serialize)]
struct RegisterIn<'a> {
    name: &'a str,
    labels: &'a [String],
    #[serde(skip_serializing_if = "Option::is_none")]
    agent_id: Option<&'a str>,
    exports: &'a [Export],
}

#[derive(Deserialize)]
struct RegisterOut {
    claim_code: String,
}

#[derive(Serialize)]
struct ClaimIn<'a> {
    code: &'a str,
}

#[derive(Deserialize)]
struct ClaimOut {
    agent_id: String,
    credential: Credential,
}

#[derive(Serialize)]
struct ExportsIn<'a> {
    exports: &'a [Export],
}

#[derive(Deserialize)]
struct ExportsOut {
    exports: Vec<Reported>,
}

#[derive(Serialize)]
struct ImportIn<'a> {
    under: &'a [String],
}

#[derive(Deserialize)]
struct ImportOut {
    projects: Vec<Listed>,
}

#[derive(Clone)]
pub struct HttpApi {
    client: Client,
    base: Url,
}

impl HttpApi {
    // A redirect would carry a claim code elsewhere, and breaks a signature anyway.
    pub fn new(base: Url) -> Result<Self, AgentError> {
        Ok(Self {
            client: Client::builder()
                .timeout(TIMEOUT)
                .redirect(Policy::none())
                .user_agent(concat!("enggraph-agent/", env!("CARGO_PKG_VERSION")))
                .build()?,
            base,
        })
    }

    /// Asks an operator's question: this agent may not be enrolled yet, so nothing is signed.
    pub async fn importable(
        &self,
        token: &str,
        under: &[String],
    ) -> Result<Vec<Listed>, AgentError> {
        let response = self
            .client
            .post(agents_url(&self.base, "import")?)
            .bearer_auth(token)
            .json(&ImportIn { under })
            .send()
            .await?;
        if matches!(
            response.status(),
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
        ) {
            return Err(AgentError::Api(
                "the stack takes an operator token for this, and that was not one".into(),
            ));
        }
        let out: ImportOut = decoded(accepted(response, StatusCode::OK)?).await?;
        Ok(out.projects)
    }

    async fn signed(
        &self,
        method: Method,
        agent_id: &str,
        credential: &Credential,
        action: &str,
        body: Vec<u8>,
    ) -> Result<Response, AgentError> {
        let url = agents_url(&self.base, &format!("{agent_id}/{action}"))?;
        let timestamp = unix_now().to_string();
        let signature = sign(
            &credential.key,
            &timestamp,
            method.as_str(),
            url.path(),
            &body,
        )?;
        Ok(self
            .client
            .request(method, url)
            .header("Content-Type", "application/json")
            .header("X-Credential", &credential.header)
            .header("X-Timestamp", timestamp)
            .header("X-Signature", signature)
            .body(body)
            .send()
            .await?)
    }
}

impl Api for HttpApi {
    async fn register(
        &self,
        name: &str,
        labels: &[String],
        agent_id: Option<&str>,
        exports: &[Export],
    ) -> Result<String, AgentError> {
        let response = self
            .client
            .post(agents_url(&self.base, "register")?)
            .json(&RegisterIn {
                name,
                labels,
                agent_id,
                exports,
            })
            .send()
            .await?;
        let out: RegisterOut = decoded(accepted(response, StatusCode::CREATED)?).await?;
        Ok(out.claim_code)
    }

    async fn claim(&self, code: &str) -> Result<Claim, AgentError> {
        let response = self
            .client
            .post(agents_url(&self.base, "claim")?)
            .json(&ClaimIn { code })
            .send()
            .await?;
        match response.status() {
            StatusCode::CONFLICT => return Ok(Claim::Waiting),
            StatusCode::UNAUTHORIZED => return Ok(Claim::Refused),
            _ => {}
        }
        let response = accepted(response, StatusCode::OK)?;
        let signature = response
            .headers()
            .get("X-Signature")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
            .ok_or_else(|| AgentError::Api("the claim response carries no signature".into()))?;
        let body = response.bytes().await?;
        verify_claim(code, &body, &signature)?;
        // A decode error may quote the body, and this one holds a key.
        let out: ClaimOut = serde_json::from_slice(&body)
            .map_err(|_| AgentError::Api("the claim response is malformed".into()))?;
        Ok(Claim::Granted {
            agent_id: out.agent_id,
            credential: out.credential,
        })
    }

    async fn refresh(
        &self,
        agent_id: &str,
        credential: &Credential,
    ) -> Result<Signed<Credential>, AgentError> {
        let response = self
            .signed(Method::POST, agent_id, credential, "credential", Vec::new())
            .await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            return Ok(Signed::Refused);
        }
        Ok(Signed::Accepted(
            decoded(accepted(response, StatusCode::OK)?).await?,
        ))
    }

    async fn put_exports(
        &self,
        agent_id: &str,
        credential: &Credential,
        exports: &[Export],
    ) -> Result<Signed<Vec<Reported>>, AgentError> {
        let body = serde_json::to_vec(&ExportsIn { exports })
            .map_err(|err| AgentError::Api(err.to_string()))?;
        let response = self
            .signed(Method::PUT, agent_id, credential, "exports", body)
            .await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            return Ok(Signed::Refused);
        }
        let out: ExportsOut = decoded(accepted(response, StatusCode::OK)?).await?;
        Ok(Signed::Accepted(out.exports))
    }
}

/// The address of everything under `/agent/agents/`; that path is what gets signed.
pub fn agents_url(base: &Url, rest: &str) -> Result<Url, AgentError> {
    base.join(&format!("agent/agents/{rest}"))
        .map_err(|err| AgentError::Api(format!("{rest}: {err}")))
}

fn accepted(response: Response, expected: StatusCode) -> Result<Response, AgentError> {
    if response.status() == expected {
        Ok(response)
    } else {
        Err(AgentError::Api(format!(
            "{} answered {}",
            response.url().path(),
            response.status()
        )))
    }
}

async fn decoded<T: DeserializeOwned>(response: Response) -> Result<T, AgentError> {
    let path = response.url().path().to_owned();
    let body = response.bytes().await?;
    // A decode error may quote the body, and a credential holds a key.
    serde_json::from_slice(&body).map_err(|_| AgentError::Api(format!("{path} answered nonsense")))
}

fn mac(key: &[u8]) -> Result<HmacSha256, AgentError> {
    HmacSha256::new_from_slice(key).map_err(|err| AgentError::Api(err.to_string()))
}

/// HMAC-SHA256 of `ts\nMETHOD\npath\nsha256(body)` under the credential's key.
pub fn sign(
    key: &str,
    timestamp: &str,
    method: &str,
    path: &str,
    body: &[u8],
) -> Result<String, AgentError> {
    let message = format!(
        "{timestamp}\n{method}\n{path}\n{}",
        hex::encode(Sha256::digest(body))
    );
    let mut signer = mac(key.as_bytes())?;
    signer.update(message.as_bytes());
    Ok(hex::encode(signer.finalize().into_bytes()))
}

/// A claim answer is signed under the code, so it can only come from the stack that issued it.
pub fn verify_claim(code: &str, body: &[u8], signature: &str) -> Result<(), AgentError> {
    let wrong = || AgentError::Api("the claim response signature does not match".into());
    let signature = hex::decode(signature).map_err(|_| wrong())?;
    let mut check = mac(code.as_bytes())?;
    check.update(body);
    check.verify_slice(&signature).map_err(|_| wrong())
}

/// What an agent logs of its claim code and the dashboard shows beside it.
pub fn fingerprint(code: &str) -> String {
    let mut digest = hex::encode(Sha256::digest(code.as_bytes()));
    digest.truncate(FINGERPRINT_CHARS);
    digest
}

pub fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}

#[cfg(test)]
mod tests;
