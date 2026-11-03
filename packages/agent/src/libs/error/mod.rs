#[derive(Debug, thiserror::Error)]
pub enum AgentError {
    #[error("config: {0}")]
    Config(String),
    #[error("state: {0}")]
    State(String),
    #[error("api: {0}")]
    Api(String),
    #[error("socket: {0}")]
    Socket(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Http(#[from] reqwest::Error),
}

#[cfg(test)]
mod tests;
