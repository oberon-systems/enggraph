mod libs;

use std::process::ExitCode;

fn main() -> ExitCode {
    libs::cli::main()
}
