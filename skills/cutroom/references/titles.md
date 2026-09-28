# Titles (names and numbers on screen, when requested)

Designed text that appears when a name, place, price or number is spoken. Write it as an ASS file timed with `render.source_to_output` (how to call it: SKILL.md, Helpers), and point the EDL's `ass` field at it: `render.py` burns it after overlays and before subtitles, without the subtitle force_style, so its fonts, positions and tags survive.

- **Confirm every name before it goes on screen.** The ASR spells names by ear. In the first project 5 of 12 titles came out wrong (Ultraviolence heard as «Ультра Вайлет», Lust for Life as «Last for Life», Honeymoon as «Ханни Мун», plus a café and a brand only the speaker could spell). Correct what you can verify, list the rest for the user with your best reading.
- **Pick the font from a board, not from a list.** Render 2–3 candidate styles on 2–3 real frames at different shot sizes (`ffmpeg -ss T -i preview.mp4 -frames:v 1 -vf ass=candidate.ass`), stack them, show the user. Choosing from names alone is guessing.
- **Ink follows the background of the title zone.** White with a shadow disappeared on light wallpaper; near-black ink read cleanly. Sample the zone before choosing.
- **Place against the tightest shot.** With reframing the head moves: measure clearance on the closest frame (1.25× raised the hair line ~90 px). Vertical platforms cover roughly the top 220 px and the bottom 25–30% with UI; keep the main line out of the top band if the video goes to Reels or TikTok.
- **Small labels need weight.** A 30 px label at 1080 wide with 31% transparency vanished at phone size; 34 px bold at ~20% transparency read. ASS alpha runs `&H00` opaque to `&HFF` transparent.
- **Fast lists: keep the label, swap the value.** Three album names a second apart do not fit as a stack above a head; a fixed artist line with the album name swapping underneath does.

Worked style from that project: main line PT Serif Italic 88 px, label PT Sans Bold 34 px with 7 px spacing, ink `&H00181B1E`, bottom-centre anchors at y 290 / 194 on 1080×1920, `\fad(250,300)` with a 97→100% scale-in eased by `\t(0,450,0.5,...)`.
