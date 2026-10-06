//! Long-term memory's "is this the same memory?" check (`bot/memory.py`).
//!
//! `find_duplicate` compared a new memory with every stored one in Python: a per-character
//! normalisation of each text, then set arithmetic on the words. py-spy put that at a
//! quarter of all background-loop CPU (docs/perf/hotspots.md). It is pure text work, so
//! it moved here whole; the Python twin stays as the fallback and as the parity reference.

use std::collections::HashSet;

/// `str.isdigit()`: the decimal digits plus the 128 characters Unicode also calls digits
/// (superscripts, circled numbers, Ethiopic and Kayah digits). Python's `any(c.isdigit())`
/// in `_numbers` sees exactly this set.
static DIGIT: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();

fn digit() -> &'static regex::Regex {
    DIGIT.get_or_init(|| {
        regex::Regex::new(concat!(
            r"[\p{Nd}",
            r"\u{00B2}-\u{00B3}\u{00B9}\u{1369}-\u{1371}\u{19DA}\u{2070}\u{2074}-\u{2089}",
            r"\u{2460}-\u{2473}\u{2474}-\u{247B}\u{2488}-\u{248B}\u{248D}-\u{2495}\u{24EA}",
            r"\u{24F5}-\u{24FE}\u{24FF}-\u{2502}\u{2776}-\u{277F}\u{2780}-\u{2793}",
            r"\u{10A40}-\u{10A43}\u{10E60}-\u{10E68}\u{11052}-\u{1105A}\u{1F100}-\u{1F10A}]",
        ))
        .expect("digit set compiles")
    })
}

/// `_norm(text).split()`: apostrophes dropped, everything `str.isalnum()` says no to turned
/// into a space, the rest lowercased, runs of spaces collapsed.
///
/// After the replacement the only whitespace left is U+0020 (nothing that lowers to a space
/// is alphanumeric), so splitting on ASCII spaces is exactly Python's `str.split()`.
pub fn normalise_words(text: &str) -> Vec<String> {
    let stripped = text.replace(['\'', '\u{2019}'], "");
    let spaced = crate::not_alnum().replace_all(&stripped, " ");
    spaced
        .to_lowercase()
        .split(' ')
        .filter(|word| !word.is_empty())
        .map(str::to_string)
        .collect()
}

/// The digit-bearing words of a normalised memory, in order (`_numbers`).
fn numbers(words: &[String]) -> Vec<&str> {
    words
        .iter()
        .filter(|word| digit().is_match(word))
        .map(String::as_str)
        .collect()
}

/// The index of the first `(status, content)` row that says what `target` says, or -1.
///
/// Same order of tests, in the same order over the rows, as the Python: exact normalised
/// equality first, then "the words overlap enough and the numbers match exactly".
pub fn memory_duplicate(target: &str, rows: &[(String, String)], overlap: f64) -> i64 {
    let target_words = normalise_words(target);
    if target_words.is_empty() {
        return -1;
    }
    let target_set: HashSet<&str> = target_words.iter().map(String::as_str).collect();
    let target_numbers = numbers(&target_words);

    for (index, (status, content)) in rows.iter().enumerate() {
        if status != "pending" && status != "approved" {
            continue;
        }
        let other_words = normalise_words(content);
        if other_words == target_words {
            return index as i64;
        }
        if numbers(&other_words) != target_numbers {
            continue;
        }
        let other_set: HashSet<&str> = other_words.iter().map(String::as_str).collect();
        let shared = target_set.intersection(&other_set).count();
        let together = target_set.union(&other_set).count().max(1);
        if shared as f64 / together as f64 >= overlap {
            return index as i64;
        }
    }
    -1
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rows(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(s, c)| (s.to_string(), c.to_string()))
            .collect()
    }

    #[test]
    fn digits_are_the_ones_pythons_isdigit_accepts() {
        for word in [
            "8080",
            "1",
            "007",
            "١٢٣",
            "²",
            "³",
            "①",
            "๑",
            "abc123def",
            "ᵿ6",
        ] {
            assert!(digit().is_match(word), "{word}");
        }
        for word in ["", "port", "½", "Ⅷ", "twelve"] {
            assert!(!digit().is_match(word), "{word}");
        }
    }

    #[test]
    fn normalising_drops_apostrophes_and_punctuation() {
        assert_eq!(
            normalise_words("It's a  Bunch--of 42 Things!"),
            vec!["its", "a", "bunch", "of", "42", "things"]
        );
        assert_eq!(normalise_words("the\u{2019}s port"), vec!["thes", "port"]);
        assert_eq!(normalise_words("---"), Vec::<String>::new());
    }

    #[test]
    fn an_identical_wording_is_the_same_memory() {
        let r = rows(&[("approved", "The port is 8080")]);
        assert_eq!(memory_duplicate("the port is 8080!", &r, 0.85), 0);
    }

    #[test]
    fn different_numbers_make_it_a_different_memory() {
        let r = rows(&[("approved", "The port is 8080")]);
        assert_eq!(memory_duplicate("The port is 8081", &r, 0.85), -1);
    }

    #[test]
    fn a_different_number_inside_an_otherwise_shared_sentence_still_matters() {
        let r = rows(&[("approved", "backups run nightly at 3 am")]);
        assert_eq!(
            memory_duplicate("backups run nightly at 4 am", &r, 0.85),
            -1
        );
    }

    #[test]
    fn rejected_rows_are_not_candidates() {
        let r = rows(&[
            ("rejected", "The port is 8080"),
            ("approved", "The port is 8081"),
        ]);
        assert_eq!(memory_duplicate("The port is 8081", &r, 0.85), 1);
        assert_eq!(memory_duplicate("The port is 8080", &r, 0.85), -1);
    }

    #[test]
    fn the_first_match_wins_even_when_a_later_row_is_exact() {
        // Same order of tests as the Python: row 0 matches on overlap before row 1 is read.
        let r = rows(&[
            ("approved", "the gpu has 24 gb of video memory"),
            ("approved", "the gpu has 24 gb"),
        ]);
        assert_eq!(memory_duplicate("the gpu has 24 gb of memory", &r, 0.85), 0);
    }

    #[test]
    fn an_empty_target_matches_nothing() {
        let r = rows(&[("approved", "anything at all")]);
        assert_eq!(memory_duplicate("!!!", &r, 0.85), -1);
    }
}
