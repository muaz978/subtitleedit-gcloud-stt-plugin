# Contributing

Thank you for helping. Bug reports, questions, fixes and improvements to the guides are all welcome. By
taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Questions and bugs:** open an issue with one of the forms. Read the [README](README.md) and
  [docs/lessons.md](docs/lessons.md) first; many surprises are explained there.
- **Security problems:** do not open an issue. Follow [SECURITY.md](SECURITY.md).
- **Larger changes:** open an issue first, so we agree on the direction before you spend time on it.

## Rules for this repository

This repository is public, so these rules are checked in review. The CI workflow enforces two of them: no
em dashes or en dashes in Markdown files, and no private key text in any file.

- Never commit keys, settings values, cloud project, bucket or account names, email addresses, local or
  drive paths, episode or show titles, or any subtitle, notes or transcript from a real recording. Describe
  patterns, not recordings. Tests use invented text only.
- When a recording teaches something general, add it to [docs/lessons.md](docs/lessons.md) as a rule. When
  the way of working changes, update [docs/episode-workflow.md](docs/episode-workflow.md). Keep both free of
  recording details.
- No em dashes or en dashes in documentation, notes or commit messages. Restructure the sentence.
- Commit messages and pull requests carry no AI attribution lines.
- Keep fixes minimal, and add a regression test for each new defect, built from observed values.

## Setting up

You need Python 3.9 or newer for the tools, and the .NET 10 SDK for the plugin. `ffmpeg`, `ffprobe` and the
Google Cloud CLI are needed only to run the tools for real, not to run their tests.

```bash
git clone https://github.com/muaz978/subtitleedit-gcloud-stt-plugin.git
cd subtitleedit-gcloud-stt-plugin
```

Run the tests of the tools. They use synthetic data only: no network, no media and no `gcloud`.

```bash
python3 -B -m unittest discover tools/tests
```

On Windows, use `python` in place of `python3`.

Build and test the plugin:

```bash
dotnet test tests/SubtitleEdit.GoogleCloudStt.Tests
./scripts/publish.sh osx-arm64    # or linux-x64, win-x64 and so on; needs bash and zip, and recreates dist/
```

Some plugin tests need real media or real word timings and return early without them, so they count as
passed even though they did not run. Set `SE_STT_SAMPLE_DIR` to any folder of `.mp4` files to run the media
tests. The ground truth test needs the maintainer's private file of 13,175 word timings, so only the
maintainer can run it. The plugin builds are untrimmed on purpose; read the notes in the README before
changing how they are published.

## Commits and pull requests

1. Fork the repository and create a branch from `main`.
2. Make a small, focused change, with a test for any defect you fix.
3. Write a commit message that says what changed and why, in the imperative mood, in plain sentences.
4. Open a pull request and fill in the template. The CI workflow must pass.

I review every pull request and may ask for changes. I may squash commits when merging. Contributions are
accepted under the repository's [MIT license](LICENSE).
