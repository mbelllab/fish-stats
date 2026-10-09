# Fish Stats

Stats about [No Such Thing As A Fish](https://www.nosuchthingasafish.com/), worked out from
transcripts of every episode: who talks most, time to penis, every headline fact, friends
and enemies of the pod, catchphrases, swears, a word tracker and more. Live at
https://fishstats.belllab.ca. An unofficial fan project.

This is the code only: no audio, transcripts or voice samples.

## How it works

733 episodes, transcribed on one home PC with free tools.

1. **Transcribe:** [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (`large-v3-turbo`,
   on the GPU) for the words, [WhisperX](https://github.com/m-bain/whisperX) to align them,
   [pyannote](https://github.com/pyannote/pyannote-audio) to split the speakers. About 6
   minutes per episode on an AMD RX 6750 XT.
2. **Name the voices:** the transcriber only knows "Speaker 1", "Speaker 2"... so each voice
   is compared with voiceprints of the four hosts (a few clips of each), using the episode's
   audio. A host is named when the match is strong and clearly beats the next best.
   Guests have no voiceprint, so they're named by elimination: once the hosts are found,
   any voice left that talks for at least a minute gets a name from the episode
   description, from the part that lists who's on ("Dan, James, Andy and Zoe Lyons
   discuss..." or "Zoe Lyons joins Dan..."). The voice that talks most gets the name the
   description backs best. With one voice left and one guest listed, it can only be them;
   otherwise the name is shown as a guess ("Zoe Lyons?"). Guest naming only runs once all
   the hosts have voiceprints, or a host could be taken for the guest.
3. **Find the facts:** from how the show hands them over ("fact number two, and that is
   Anna... My fact this week is...").
4. **Count:** everything goes into SQLite, and each stats page's numbers are worked out from
   it. Friends and enemies mentions were read by hand (`curated/pod_friends.json`).

## Make your own

Python 3 is all you need for the site.

**From your own transcripts.** Name each file after the episode, starting with the date
(`2024-03-14 - 522. No Such Thing As Monet's Bog Cottons.vtt`). If the speakers are named
(`<v Dan>`, `[Dan]` or `Dan:`), import them:

    python scripts/import_transcripts.py "No Such Thing As A Fish" ~/transcripts/*.vtt

Then build and look:

    python scripts/build.py
    cd site && python -m http.server 8000

**From audio.** Needs a GPU (only tested on AMD under Linux). Install PyTorch for your card
and `whisperx==3.8.6`, build whisper.cpp with Vulkan into `scripts/whispercpp/`, accept the
terms of pyannote's gated models on HuggingFace and put a token in `scripts/hf_token.txt`.
Put the mp3s in `data/No Such Thing As A Fish/mp3s/`, then:

    bash scripts/run_transcribe.sh

It starts a heat guard first, which pauses the work if the card gets too hot and resumes
when it cools. It was added after a blocked fan let the card overheat and shut the whole
PC down mid-batch. The fan was fixed, but the guard stays as a safety net. It reads AMD's
sensors; to run without it, start with `NO_GPU_GUARD=1`.

To name voices, put a clip of each host in `data/voiceprints/` (`Dan.mp3`) and run
`python scripts/enroll_speakers.py`; the build does the rest.

## Licence

MIT No Attribution: use it for anything, no credit needed.
