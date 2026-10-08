# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `tools/qc`: helper scripts for re-reading, comparing, voting on and patching a transcript, with tests.
- The second re-read and vote method in the episode workflow, and new lessons on archaic or dialect speech,
  repeats lost to the loop collapse, retiming a block, and the faults of the repair step.
- Privacy policy, security policy, contributing guide and code of conduct.
- Issue forms, a pull request template, a code owners file, Dependabot configuration and a continuous
  integration workflow that runs the tests of the plugin and the tools.

### Changed

- The plugin's media tests read their sample paths from `SE_STT_SAMPLE_DIR` and
  `SE_STT_GROUND_TRUTH_WORDS`, and return early when those are not set.
- Comments in the standalone tool about the repair step were corrected, and the README cost table was fixed.

## [1.1.0] - 2026-10-05

### Added

- `tools/transcribe-broadcast.py`, a tool for recordings in which two languages alternate. It recognizes
  the audio once per language and again in short windows with speaker labels, decides the language of each
  stretch word by word, and writes a subtitle, a word level file, an events file and a notes file.
- The standalone single language tool, the step by step episode workflow and the field guide to `chirp_3`
  are part of this tag's source archive for the first time.

The Subtitle Edit plugin is unchanged since 1.0.0, with the same version and the same files.

## [1.0.0] - 2026-09-05

### Added

- The Subtitle Edit 5 plugin: transcribes the open video with Google Speech-to-Text v2 (`chirp_3`) and
  uses real word level timings, so cues land on measured speech instead of being spread by character
  count. Self contained builds for Windows, macOS and Linux (x64 and arm64).

[Unreleased]: https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/releases/tag/v1.0.0
