use std::io::{Error, ErrorKind};

use super::AgentError;

#[test]
fn a_message_says_which_part_failed() {
    assert_eq!(
        AgentError::Config("alpha".into()).to_string(),
        "config: alpha"
    );
    assert_eq!(AgentError::State("beta".into()).to_string(), "state: beta");
    assert_eq!(AgentError::Api("gamma".into()).to_string(), "api: gamma");
    assert_eq!(
        AgentError::Socket("delta".into()).to_string(),
        "socket: delta"
    );
}

#[test]
fn an_io_error_keeps_its_own_words() {
    let error = AgentError::from(Error::new(ErrorKind::NotFound, "epsilon"));

    assert_eq!(error.to_string(), "epsilon");
}
