# Working in this repository

For AI assistants and for people. Claude Code reads CLAUDE.md, which points here.

## Start here

- To transcribe a video or audio file, follow docs/episode-workflow.md with tools/transcribe-episode.py.
  Read docs/lessons.md once before a first long run.
- Run the tool on the user's own machine, with the user's own settings. Never ask for the service
  account key, never paste it into a conversation, and never print its contents.
- If you cannot run commands, help the user work through the notes file the tool wrote, using
  docs/lessons.md.

## Rules for this repository

- Episode work ("work on this ep" with a video path): follow docs/episode-workflow.md.
- This repository is public. Never commit keys, settings values, cloud project, bucket or account
  names, email addresses, local or drive paths, episode or show titles, or any subtitle, notes or
  transcript from a real recording. Describe patterns, not recordings.
- When an episode teaches something general, add it to docs/lessons.md as a rule. When the way of
  working changes, update docs/episode-workflow.md. Keep both free of recording details.
- No em dashes or en dashes in documentation, notes or commit messages. Restructure the sentence.
- Commit messages and pull requests carry no AI attribution lines.
- Keep fixes minimal, and add a regression test for each new defect, built from observed values.
