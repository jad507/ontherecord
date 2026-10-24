# Checking transcripts by ear (the Check page)

One speaker turn per screen: listen, fix what Whisper got wrong, then code stretches of the turn. Words Whisper was
unsure of are underlined with dots, and for each of them it offers the words it would have said instead, so most
fixes are a single key on the left side of the keyboard. Built Oct 24, 2026 as five prototypes to try against each
other; the page itself has a tutorial for each (practice turns, nothing saved).

## Making a set

```bash
ontherecord submit recipes/check-words.yaml --wait        # word_choices on cached transcripts (a GPU job)
ontherecord review new RUN_ID --kind check                # every speaker turn of the run, in order
ontherecord review new RUN_ID --kind check --codes "Public comment, Question, Request"   # name your codes
```

Then open `http://127.0.0.1:8765/check#SET_ID` (the Review tab's Start button goes there too). A recipe needs
`audio` and `align` before `word_choices`; see `recipes/check-words.yaml`. Turns are at most 60 seconds
(`--turn-seconds`); a longer turn is split into parts at line boundaries ("part 2 of 5"). `--sample N` takes N turns
at random, kept in order. Codes go on keys 1-5 then Q-T, in the order given (ten at most); without `--codes` the set
gets ten placeholder names for a council meeting.

## What the word choices cost

`word_choices` does not transcribe again. For each stretch of up to 30 seconds it aligns the transcript's own words to
the audio (times, and Whisper's probability for each token), then for each word with a token below 0.8 it asks Whisper
what else it would have said there, and completes each alternative for a few words to see how many words it replaces
("gonna" for "going to", "200,000" for "two hundred thousand", nothing for a word Whisper invented).

Measured on shiro (RTX 5070 Ti, large-v3 float16, Oct 24): 85 s for the 11-minute water mains meeting and 154 s for
the 36-minute press conference, so 4 to 8 minutes of GPU per hour of audio, against about 9 for the transcript
itself. 12% and 9% of words were doubtful; 8% and 5% got choices. Most of the time goes on the completions, because
CTranslate2 decodes a forced prefix one token at a time. Run it on the recordings someone is about to check, not
on everything.

## The five ways of working

| | How you fix a word | Keys | Good for |
|---|---|---|---|
| 1. Follow along | The audio plays and the highlight follows it. Q W E R pick for the word under the highlight; 1 2 3 4 pick for the doubtful word just passed (you are usually a word or two late). 5 makes it stop after each doubtful word. | Space play/pause, A/F step a word, T type, X delete, V can't make out, C undo, S save, D code | listening straight through |
| 2. Key per word | Every doubtful word has its own key, in reading order across the keyboard (1-5, Q-T, A-G). Press it to flip through what it might be; Shift flips back. | Space, Z from start, ` then a key to type, X undo, C code, V save, Tab next 15 words | reading faster than listening |
| 3. Grid | One line at a time. Under the line, each doubtful word is a column and each choice a row, laid out like the keyboard: 1 is the first word's first choice, W the second word's second. One press is an exact pick. | Tab next line, Space/Z play the line, ` type the line, X undo, C code, V save | seeing every option at once |
| 4. One at a time | Like a spelling checker: it rings one doubtful word, plays it, and lists 1 keep, 2-5 the choices, T type, X delete. Picking moves on. | Space again, A back, F skip, G whole turn, C undo, S save, D code | first-timers: everything is on screen |
| 5. Type it | A text box with the transcript; the grey line above is what was heard. Typing pauses the audio, Esc plays (going back a second), Tab swaps the word at the cursor for Whisper's other guesses. | Esc, Tab, Alt+A play from cursor, Alt+Z back 3 s, Alt+C code, Ctrl+Enter save | fast typists; fixing anything, not just doubtful words |

All five save the same thing, so they can be compared. Clicking a word types over it in every way. Keys are read
by position (`KeyboardEvent.code`), so the layout holds on a non-US keyboard.

**Coding** (the same in every way): drag across words with the mouse, or move with A and F holding Shift to
stretch; press a code's key to add it and again to take it off. Codes stack. With nothing selected (Esc), a code
applies to the whole turn. Each code has its own color and line style (solid, dashed, dotted, double), and its key
shows where it starts, so colors are not the only cue.

## What is recorded

Each saved turn appends an answer to the set's `answers.jsonl`: the corrected text, its words (each pointing at the
transcript word it came from, or marked hard to make out), every edit and how it was made (`choice`, `typed`,
`deleted`), the codes as word spans, which way of working did it, keys pressed, plays, and the seconds spent, counted
only while someone is pressing keys, clicking or listening (a page left idle does not count). Unsaved work is kept in
the browser as a draft until saved. `ontherecord review score SET` prints:

- the word error rate of Whisper's text against the corrected text. The person saw Whisper's text, so this
  understates Whisper's errors (people anchor on what they see); for an unbiased figure use
  `review new RUN --kind transcript --blind`;
- edits by kind, and how often someone typed a word that was one of the choices (a choice that was missed or hard to
  reach);
- per way of working: turns, minutes of audio, and seconds per minute of audio, which is the comparison to make.

## Notes

- The choices' percentages are Whisper's own probability of that token at that point, given the transcript's words
  before it and the audio of that line. They are not calibrated against people; a check set can measure that.
- Choices include formatting ("two" or "2"), which matters for some transcripts and is noise for others.
- Colors follow Penn State's palette (Nittany Navy, Beaver Blue, Pugh Blue and the PA accents), with darker accent
  variants in the light theme where the brand color would fall below WCAG AA contrast. Every text color is at least
  4.5:1 on its background in both themes; the theme follows the system unless picked in the header. Text is in a book
  serif the machine already has (Charter on a Mac, Sitka or Georgia on Windows, Noto Serif on most Linux), because
  people who are not programmers find sans serif and console-like type off-putting; nothing is fetched.
