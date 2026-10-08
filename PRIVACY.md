# Privacy policy

Last updated: 8 October 2026

## In short

- The maintainer of this repository collects nothing from you. The plugin and the tools have no server of
  their own, no accounts, no telemetry, no analytics, no crash reporting and no update check.
- The software runs on your computer. To transcribe, it sends your audio to Google Cloud, using your own
  Google Cloud project and credentials. That is what it is for, and it means your recordings leave your
  computer.
- Copies of your recordings and transcripts are also kept on your computer, and sometimes for a while in your
  Cloud Storage bucket. This page says where, and how to remove them.

This page describes what the code in this repository does. It is not legal advice.

## Who is responsible

The repository is maintained by the GitHub user [muaz978](https://github.com/muaz978). The software is
yours to run: the maintainer never receives, stores or processes your recordings, transcripts or
credentials. When you run it, Google Cloud processes your data under the agreement between you and Google,
and you choose the project and the account that are used. With the tools you also name the bucket and the
region. The plugin window has no bucket or region field, so it uses a bucket named after your project and the
`us` region unless you change its saved settings.

## What is sent, and to whom

The same for the Subtitle Edit plugin and for the standalone tools:

- **Your audio**, converted on your computer to 16 kHz mono FLAC and cut into chunks (about 18 minutes each
  by default, 10 minutes in the two language tool, and shorter pieces when a stretch is re-read). The video
  file itself is not uploaded.
- It goes first to a **Cloud Storage bucket in your own project**, and then **Google Speech-to-Text v2**
  reads it from there. The request also carries the language code, the model name (`chirp_3` by default),
  the flags for word timings and punctuation, the processing strategy, and your project id and region
  (`us` by default). The two language tool also asks for speaker labels in one of its jobs, and it requests
  Arabic and English instead of the language you configured.
- The **transcript with word timings** comes back in the response. No result files are written to your
  bucket.
- Nothing is sent to the maintainer or to any other server. The only network destinations in the code are
  Google Cloud's Speech-to-Text and Cloud Storage services and the Google sign in service that the Google
  libraries use to obtain an access token.

### How you sign in

- **Plugin.** You give the path of a service account key file. Google's own authentication library loads
  it. The plugin does not store, copy, log or send the contents of the key; it reads only the project id
  from it, to fill in a field. The path of the file, the project id, the bucket name, the language, the model,
  the region and the dynamic batching choice are returned to Subtitle Edit, which saves them with its own
  settings, in plain text.
- **Tools.** They never open a key file. They ask the Google Cloud CLI for a short lived access token
  (`gcloud auth print-access-token`) and keep it in memory only. Uploads and deletions go through
  `gcloud storage`. If you do not name a service account, the tools use whichever `gcloud` account is
  active, which may be your personal one: use a dedicated service account instead. Error text is stripped of
  project, bucket and account names, `gs://` paths and email addresses before it is printed, but read any log
  before you share it.

### What the plugin creates in your project

If the bucket does not exist, the plugin creates one named after your project id with the suffix
`-subtitle-edit-stt`. A bucket the plugin creates gets a lifecycle rule that deletes objects once they are a day old (Google applies lifecycle rules with some
delay).
Objects are named `subtitle-edit/<UTC date and time>-<random id>/<file name>.flac`, where the file name is
`audio.flac`, `part-000.flac` or similar; the name of your video file is never used. The tools use the bucket you name and put objects under `stt/`.

## What stays on your computer

**Plugin**

- The extracted audio and its chunks are written, in a folder called `transcription`, under the temporary
  folder that Subtitle Edit gives the plugin (or under the system temporary folder if it gives none). The
  plugin does not delete them itself and relies on Subtitle Edit to remove its folder. Delete the
  `transcription` folder yourself if you want to be sure.
- The plugin writes one response file for Subtitle Edit, which holds the subtitle text and the settings.

**Standalone tools**

- A work folder, by default `~/.cache/se-stt/<name made from the video file name>`, holds the full audio and
  its chunks as FLAC, the complete responses from Google (which contain the transcript text), records of
  pending operations (project path, bucket name, upload location), the list of upload locations, a copy of
  the subtitle from before recovered words were added (`before-recovery.srt`, when words were recovered),
  and `report.json`. Treat every file in this folder as possibly holding your recording or its transcript.
  The one exception is `report.json`, which holds counts, timings and check results and neither transcript
  text nor the video's name. The two language tool puts its work folder next to its output instead, and
  keeps the words of every job (`result.json`) and a log of each job in it.
- The tools write the subtitle and a notes file where you tell them to, next to the video by default. The
  notes file can quote up to eight words for each recovered stretch. The two language tool also writes a file
  with every word, its language and its speaker (`.words.json`) and a file of events (`.events.json`). The
  re-read helper `tools/qc/recheck.py` writes the re-read words to a file you name.
- The settings file `~/.config/se-stt/config.env` holds your project, bucket, language, optional service
  account name, and optional region and model. The tools read it and never write it. It holds no key.
- Nothing in these folders is deleted automatically, apart from the short recovery pieces and the records of
  finished operations. **To remove your data, delete the work folder and the output files.** First look under
  `stt/` in your bucket and delete what is left there, because the work folder holds the only record the
  tools have of what a later run should remove.

## What stays in your bucket, and for how long

- **Plugin.** At the end of a run the plugin deletes the objects it uploaded. If you close the window during
  a run, the process is stopped, or a deletion fails (failures are ignored), audio can remain. Only a bucket
  that the plugin created itself has the one day lifecycle rule. For a bucket that already existed, add your
  own rule.
- **Tools.** At the end of every run, including after an error or an interruption, they delete the audio they
  uploaded. Audio can remain if the process is killed abruptly or the computer loses power (a later run in
  the same work folder removes what it recorded), if a deletion fails (a warning is printed and a later run
  tries again), and while an operation that is still waiting at Google needs it (it is kept until a later
  run). The tools' guide recommends a bucket lifecycle rule that deletes objects under `stt/` after two
  days. Please add it.
- Cloud Storage features that you or Google switch on for a bucket, such as soft delete or object
  versioning, can keep deleted objects for a while. Check your bucket's settings.

## Google's role

Google Cloud receives the audio and returns the transcript. What Google keeps, logs or uses, and where, is
set by your agreement with Google, your project settings and the region you choose, not by this repository.
See [Google Cloud's privacy notice](https://cloud.google.com/terms/cloud-privacy-notice) and
[data processing addendum](https://cloud.google.com/terms/data-processing-addendum).

## What the maintainer can see

- Nothing from the software.
- GitHub shows the maintainer aggregate figures for the repository, such as the number of clones, views and
  release downloads. They do not name individuals.
- Issues, pull requests and comments are public and are processed by GitHub under its
  [privacy statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).
  Do not post keys, project or bucket names, email addresses or private recordings or transcripts there.
- Commits carry the maintainer's GitHub handle and its no-reply address.

## Third party components

The plugin uses Google's Speech and Storage client libraries and the Avalonia user interface library. The
tools use `gcloud`, `ffmpeg` and `ffprobe`, which you install yourself. Their own terms apply. This
repository's code does not switch on any telemetry in them; their internals have not been audited here.

## Your responsibilities

- Make sure you may process the recordings you transcribe. Voices and names are personal data in many
  places, and you are the one who decides to send them to Google.
- Use a dedicated service account with the smallest roles that work (see the README), and keep its key out
  of version control, chat and screenshots.
- Add the bucket lifecycle rule, and delete work folders and outputs you no longer need.

## Children

The software is not aimed at children and collects nothing from anyone.

## Changes

A change to this policy is a commit to this file, and the history shows when it was made.

## Contact

For a question, open an [issue](https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/issues) without
private data in it. For a security or privacy problem that should not be public, send a
[private report](https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/security/advisories/new).
