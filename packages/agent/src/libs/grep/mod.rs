use std::sync::Arc;
use std::time::{Duration, Instant};

use grep_regex::RegexMatcherBuilder;
use grep_searcher::sinks::Lossy;
use grep_searcher::{BinaryDetection, SearcherBuilder};
use ignore::gitignore::GitignoreBuilder;
use ignore::overrides::OverrideBuilder;
use ignore::WalkBuilder;
use serde::Serialize;

use crate::libs::confine::{is_denied, Root};

const MAX_LINE_BYTES: usize = 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Query {
    pub pattern: String,
    pub ignore_lines: Vec<String>,
    pub path_glob: Option<String>,
    pub limit: usize,
    pub fixed: bool,
    pub within: Duration,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Hit {
    pub path: String,
    pub line: u64,
    pub text: String,
}

fn shortened(line: &str) -> String {
    let line = line.trim_end_matches(['\n', '\r']);
    line[..line.floor_char_boundary(MAX_LINE_BYTES)].to_owned()
}

/// Searches as `MountTree.grep` has ripgrep do: every file, hidden too, case ignored.
///
/// Links are not followed, a denied name is never opened whatever the stack's ignore
/// lines say, and a file is opened through the root. Only a bad pattern is an error.
pub fn search(root: &Root, query: &Query, emit: &mut dyn FnMut(Hit) -> bool) -> Result<(), String> {
    let matcher = RegexMatcherBuilder::new()
        .case_insensitive(true)
        .fixed_strings(query.fixed)
        .line_terminator(Some(b'\n'))
        .build(&query.pattern)
        .map_err(|err| err.to_string())?;
    let mut ignores = GitignoreBuilder::new(root.path());
    for line in &query.ignore_lines {
        ignores
            .add_line(None, line)
            .map_err(|err| err.to_string())?;
    }
    let ignores = Arc::new(ignores.build().map_err(|err| err.to_string())?);
    let mut overrides = OverrideBuilder::new(root.path());
    if let Some(glob) = &query.path_glob {
        overrides.add(glob).map_err(|err| err.to_string())?;
    }
    let walker = WalkBuilder::new(root.path())
        .standard_filters(false)
        .hidden(false)
        .follow_links(false)
        .overrides(overrides.build().map_err(|err| err.to_string())?)
        .sort_by_file_name(|one, other| one.cmp(other))
        .filter_entry(move |entry| {
            entry.depth() == 0
                || !ignores
                    .matched(entry.path(), entry.file_type().is_some_and(|t| t.is_dir()))
                    .is_ignore()
        })
        .build();
    let mut searcher = SearcherBuilder::new()
        .line_number(true)
        .binary_detection(BinaryDetection::quit(0))
        .build();
    let started = Instant::now();
    let mut left = query.limit;
    for entry in walker {
        if left == 0 || started.elapsed() > query.within {
            break;
        }
        let Ok(entry) = entry else { continue };
        if !entry.file_type().is_some_and(|kind| kind.is_file()) {
            continue;
        }
        let Ok(rel) = entry.path().strip_prefix(root.path()) else {
            continue;
        };
        let Some(path) = rel.to_str() else { continue };
        if entry.file_name().to_str().is_none_or(is_denied) {
            continue;
        }
        let Ok(file) = root.open(path) else { continue };
        let mut stopped = false;
        let outcome = searcher.search_file(
            &matcher,
            &file,
            Lossy(|line, text| {
                left -= 1;
                let hit = Hit {
                    path: path.to_owned(),
                    line,
                    text: shortened(text),
                };
                stopped = !emit(hit);
                Ok(left > 0 && !stopped)
            }),
        );
        if outcome.is_err() {
            continue;
        }
        if stopped {
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
