# Security policy

## Supported versions

| Version | Supported |
|:--|:--|
| The latest release and the `main` branch | Yes |
| Older releases | No. Please update. |

The Subtitle Edit plugin is maintained on a best effort basis. Subtitle Edit 5 ships this engine built in,
and that is the better maintained way to use it from inside Subtitle Edit. The standalone tools in `tools/`
carry the most guards.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting instead:
[report a vulnerability](https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/security/advisories/new).
Describe what you found, how to reproduce it, and what it could allow. Leave real keys, project or bucket
names and private recordings out of the report; an invented example is enough.

I will acknowledge a report within a week where I can, say whether I consider it a vulnerability, and tell
you when a fix is released. I will credit you in the release notes if you wish. Please give me a reasonable
time to fix the problem before you share it.

## What is in scope

- Anything that could expose a service account key, an access token, or the contents of a recording or a
  transcript.
- Code that deletes or overwrites cloud objects or local files other than the ones the software created.
- Unsafe handling of file names, settings or environment variables, for example running unintended commands.
- Weaknesses in how the release files are built or published.

Not in scope: problems in Google Cloud, Subtitle Edit, Avalonia, `ffmpeg` or `gcloud` themselves (please
report those to their own projects), and anything that needs access to your computer or your Google account
to begin with.

## Using the software safely

- Use a **dedicated service account** with the Cloud Speech Client role. For the standalone tools, give it
  object access to one bucket only. The plugin creates its own bucket when none exists, and that needs the
  Storage Admin role; to avoid that, create the bucket yourself with the name the plugin would choose (your
  project id followed by `-subtitle-edit-stt`). Do not use your personal account or a broad role.
- Keep the key file out of version control, chat messages and screenshots. Share its path, never its
  contents.
- Add a bucket lifecycle rule that deletes the uploaded audio. The plugin sets a one day rule on a bucket it
  creates itself. For the standalone tools use two days on the `stt/` prefix, as `tools/README.md`
  explains, because a resumed operation can still need its files.
- Download releases only from this repository's
  [releases page](https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/releases). GitHub shows a SHA-256
  digest next to each file; compare it with the result of `shasum -a 256 <file>` (macOS), `sha256sum <file>` (Linux) or
  `Get-FileHash <file>` (Windows PowerShell). The macOS builds are ad-hoc signed only, and the other builds
  are not signed.

See also the [privacy policy](PRIVACY.md), which lists what the software sends and stores.
