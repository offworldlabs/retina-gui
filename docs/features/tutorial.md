# Guided tutorial

The tutorial is a tour of the pages an owner lands on after setup: Summary, Home and
Configuration. It runs over the real pages rather than over screenshots of them. One thing at a
time is lit while the rest of the page is dimmed, a card says in a sentence or two what it is and how
to use it, and an arrow joins the two. The card carries Previous, Next and Skip tutorial. To move
from one page to the next, the owner clicks the real control that goes there.

It is look-only. It writes nothing to the node, posts nothing, and needs no CSRF token: every
request it makes is a GET for a page the owner could have opened by hand.

## Where it lives

| File | Role |
| --- | --- |
| [src/tutorial.py](../../src/tutorial.py) | `STEPS` (order, wording, targets, images), `steps_for`, `first_url`, `payload` |
| [src/routes/tutorial.py](../../src/routes/tutorial.py) | `/tutorial` (the entry point) and `tutorial_context` (the per-request payload) |
| [static/tutorial.js](../../static/tutorial.js) | Draws the overlay: dimming, lit regions, arrows, the card, keyboard handling |
| [static/tutorial.css](../../static/tutorial.css) | Overlay styles, loaded only while a step lives on the page |
| [static/tutorial/](../../static/tutorial/) | The example images shown on the service steps and the two Signal peak steps |
| [templates/base.html](../../templates/base.html) | Loads the stylesheet, payload and script when `tutorial` is set |
| [templates/setup/_complete.html](../../templates/setup/_complete.html), [templates/setup.html](../../templates/setup.html) | The invitation and Start tutorial button on the wizard's last step |
| [templates/config.html](../../templates/config.html) | The Tutorial section and its Launch tutorial button |

## Starting a run

There are two ways in, and both are a plain link to `/tutorial`:

- The setup wizard's last step recommends the tour to anyone setting up a node for the first
  time. Its footer offers **Start tutorial** as the primary button and **Go to dashboard** beside
  it. See [setup-wizard.md](setup-wizard.md#completion).
- The Tutorial section of the Configuration page (in the Other group) has **Launch tutorial**,
  which runs the tour alone without re-running setup.

`/tutorial` redirects to the first step's page. While the setup wizard is in progress it
redirects to `/set-up` instead: Home and Config both bounce there until setup finishes, so a
tour started mid-wizard would lose its place on its first page change. While Auto-Calibrate is
running, the calibration hold in `app.py` sends `/tutorial` to `/config` like any other page,
which is the right answer: the tour can wait for the run.

## How a run moves

A step is addressed by its page and its number in the run: `/summary?tutorial=2`,
`/?tutorial=6`, `/config?tutorial=16`. There is no session state and nothing stored in the
browser, so a reload resumes on the step being shown and a copied link opens the same step.

On every render, `inject_globals` in `app.py` calls `tutorial_context`. With no `tutorial`
parameter that is one dictionary lookup and the page is untouched. With one, `tutorial.payload`
returns the whole run (every step, with its URL) and which step is current, and `base.html`
hands that to the page as `window.OWL_TUTORIAL` along with `tutorial.js` and `tutorial.css`.

The payload is `None`, and the plain page is rendered, unless the number names a step that lives
on the page being asked for. A stale or hand-edited link therefore shows the page rather than a
tour pointing at things that are not there. The wizard page never matches, so the tour cannot
appear over it.

`tutorial.js` moves between steps on the same page without a reload and rewrites the address
with `history.replaceState`. When the next step is on another page it navigates to that step's
URL, and the server hands the new page the same run. The server owns the order and the wording;
the script only draws.

Finish, on the last step, drops the tour and stays on Summary, whose links that step has just
pointed at. Skip tutorial (or Escape) closes the overlay where it
is and removes the parameter from the address, so a reload does not bring the tour back.

## Which steps run

`STEPS` lists every step in tour order: Summary, then Home, then Configuration, then one closing
step back on Summary.

The Summary steps run only when there is more than one node. With a single node the Summary page
has one card and nothing to choose between, so the run starts on Home. `_include_summary` in
`routes/tutorial.py` asks `discovered_nodes`, the same list the banner and the Summary cards are
drawn from, so the tour cannot disagree with the page about how many nodes there are.

The closing step is in every run (`closing=True`, which `steps_for` keeps whatever else it
drops). It is on Summary because that is where the Resources and Help links are, and Summary is
there to visit with one node as well as with several. So a single node's run starts on Home and
still ends on Summary. Getting there is an ordinary Next from the last Configuration step.

Step numbers are positions in the run, not in `STEPS`. Home's first step is step 1 on a single
node and step 6 when Summary is toured.

### Demo mode

`/tutorial?demo=1` tours Summary even on a single node and carries `demo=1` on every step URL,
so Home is drawn with its demo tower and telemetry (see `routes/home.py`). It exists so the whole
tour can be reviewed on one node, including a development machine. The wizard's Start tutorial
button passes it on when the wizard itself is in demo mode.

## Steps

Each step has a page, a title, a body of a sentence or two, one or more targets, and optionally an
example image or a link out. The wording lives in `STEPS` and nowhere else. The last step on
Summary and on Home is a [click-through step](#click-through-steps).

| Page | Step | Points at |
| --- | --- | --- |
| Summary | List of nodes | The node cards and the tabs in the banner |
| Summary | Resources | The Resources links |
| Summary | If you need help | The Help links |
| Summary | Doppler Mapping Tool | The flight-path simulator |
| Summary | Open your node (click-through) | This node's card and this node's tab |
| Home | Welcome to your node | The page heading with the site name, and this node's highlighted tab |
| Home | System information | The "Listening on" row and the Telemetry card |
| Home | Passive Radar: detections | Its service card, and white dots in the example image |
| Home | Passive Radar: expected flights | Its service card, and orange dots in the example image |
| Home | Passive Radar: do they line up? | Its service card, and a matched and an unmatched dot |
| Home | Max-Hold | Its service card, with an example image |
| Home | ADS-B Map | Its service card, with an example image |
| Home | Tracker | Its service card, with an example image |
| Home | Controller | Its service card, with a link to the wiki's Understanding Node Data page |
| Home | Open Configuration (click-through) | The Config link in the sub-nav |
| Configuration | Welcome to Configuration | The Config link in the sub-nav, the page heading and the side nav |
| Configuration | Apply your changes | The Apply changes button |
| Configuration | Mode | The Mode section and its side-nav link |
| Configuration | Wi-Fi network | The Network section and its side-nav link |
| Configuration | Cached Towers | The Cached Towers section and its side-nav link |
| Configuration | Tower | The Tower section and its side-nav link |
| Configuration | Capture: signal peak | The Signal peak meter, with an example of a good level |
| Configuration | Capture: overload | The Signal peak meter, with an example of the overload reading |
| Configuration | Capture: automatic calibration | The two calibrate buttons |
| Configuration | Capture: manual settings | The two gain reduction rows, the LNA state row and the two notch filter switches |
| Configuration | Tracking: Minimum SNR | The Tracking section and its side-nav link |
| Configuration | ADS-B settings | ADS-B Truth and tar1090, and their side-nav links |
| Configuration | SSH Access | The SSH Access section and its side-nav link |
| Summary | That's everything (closing) | The Resources and Help links again, with a link to the wiki |

After the welcome and Apply steps, the Configuration steps follow the page's own order, group by
group (see [config-editor.md](config-editor.md#page-layout)), so the tour only ever moves down
the page.

The Support group is not toured: most of it is set during setup, and nothing in it changes how
the node performs.

The body is held to a sentence or two so the card stays a caption rather than a manual, and
`tests/test_tutorial.py` enforces it. Most are one. The Configuration welcome, the Minimum SNR step and the closing
step are two, because each has a second thing to say that is not about what is lit.

The Configuration welcome says the tour skips the sections already completed during setup. That
is Node Claim, Cloud Services and How We Reach You, which the wizard asks for, and it is said so
an owner does not take a section the tour passes over for one that does not matter.

### Tuning steps

Capture gets four steps and Tracking a fifth because together they are the one piece of tuning
an owner is expected to do, and it needs an order: what to watch (the Signal peak meter), what
too far looks like, what to change (a calibration run, or the gain reduction, LNA and notch filter settings by
hand), and then Minimum SNR. The first two show an example of the meter, because on a node that
is not capturing, the meter on the page is empty.

The Minimum SNR step says the default is 7 even when the box is blank. The box is blank
whenever the merged config carries no value, and the tracker then runs on its own default,
which is set in retina-node, not in this repository. If that default changes, the step's
wording has to change with it; nothing here can check it.

The overload step shows what the page does when the radio overloads: the tuner's reading turns
to a red "overload" and the Overload help box appears under the meter (see
[config-editor.md](config-editor.md#overload-indicator)). Its picture is the page's own meter in that
state (the box is described, not shown: with it the picture was too tall for a small window), rendered on a development instance with simulated readings, because an overload cannot
be produced to order on a working node; a capture from a node that really is overloading can
replace it at any time. On a blah2 older than blah2-arm#79 an overload stops the radio instead,
and the meter reads "no signal"; the step describes the current behaviour and does not go into
that.

Calibrating and changing the settings by hand are two steps, not one lighting both. The buttons
are at the top of the Capture section and the gain rows near the bottom, further apart than a
short window is tall, so as one step the rows were off the bottom of the screen on a laptop. The
manual step lights the notch filter switches along with the gain rows, since they move the
signal level as much as the gain does.

The Apply step says "a setting on this page", which is simpler than what is true. Apply changes
submits the Radar and ADS-B groups only; the other sections each save on their own as soon as
they are changed (see [config-editor.md](config-editor.md#administration-sections)). The step
used to say "a radar setting" for that reason, and was reworded:
an owner who presses Apply after changing one of the others loses nothing, and "radar setting"
left them to work out which settings those were.

Home's welcome step points at this node's tab and calls it the highlighted one. It does not say the
tab moves to the front, because it does not: tabs are in one fixed order on every node (see
[fleet-and-naming.md](fleet-and-naming.md#fleet-order)), and which node is open is shown by the
highlight alone.

### Targets

A target is a name. The region lit for it is the box around every visible element whose
`data-tutorial` attribute lists that name, so several elements can make up one region (the two
side-nav links for ADS-B Truth and tar1090) and one step can light several regions.

Templates carry these attributes for the tutorial alone. They are deliberately not CSS classes or
ids borrowed from the page: a selector that happens to match today breaks silently when the page
is restyled, and the first anyone hears of it is a step pointing at nothing. With a dedicated
attribute, `test_every_target_exists_on_its_page` renders each page and fails when a step's
target is gone, and `test_no_token_is_left_without_a_step` fails when markup is still tagged for
a step that no longer exists.

On the Summary page the attribute is on a wrapper `div` around each group, and in the banner it
is on each tab rather than on the row. Both keep the existing markup byte-for-byte, which the
fleet tests match as strings.

On the Configuration page most fields are drawn by the `render_field` macro, which takes a
`tutorial` argument to put the attribute on a field's row. The manual step's five rows (three inputs and two switches) are named that
way.

A target can opt out of its arrow (`_target(name, arrow=False)`). The Configuration steps use
this for the sections themselves: the side-nav link gets the arrow, and the section it leads to
is lit without a second arrow crossing the form.

### Click-through steps

The tour does not carry the owner from page to page on Next. The last Summary step asks them to
click this node's card or its tab, and the last Home step asks them to click Config, so the two
moves they will make most often are ones they have made once already. A step marked
`click=True` in `STEPS` has no Next button; its lit regions take the click instead, and the
pointer changes over them to say so.

- **Only this node is lit on Summary.** With several nodes the other cards and tabs stay dimmed
  and do nothing. Another node may be running a build without the tour, and the page that
  follows has to be this node's Home.
- **The click does not follow the link.** The overlay still takes the click and sends the
  browser to the next step's own URL. A node's card and tab link to `http://ret<node_id>.local/`
  (see [fleet-and-naming.md](fleet-and-naming.md#node-addresses)), which would drop the
  `tutorial` parameter and, for someone who arrived by IP address, by the tunnel hostname or on
  a development machine, move them to a host that may not resolve. Staying on the origin that is
  serving the tour is what keeps it running everywhere.
- **Nothing else becomes clickable.** Everything outside the lit regions stays inert, as on
  every other step.
- **The click is the only way on.** There is no Next and the right arrow key does nothing on
  these steps, because the point is that the owner makes the move themselves. Previous and Skip
  tutorial still work, so the step cannot trap anyone.
- Each click-through step is the last step on its page (`test_the_pages_are_joined_by_click_through_steps`),
  so the click always changes page.

### Example images

The service steps show a picture of what the button opens, and two of the
[tuning steps](#tuning-steps) show the Signal peak meter. All but one are real captures (the exception is the overload picture, see
[Tuning steps](#tuning-steps)), supplied by
the team, and are re-encoded from their pixels when added so no capture metadata travels with the
file. `STEPS` declares each image's width and height, which the card uses to reserve the space
before the image loads; the test suite checks they match the files.

A step with a picture uses a wide card (`.tut-wide`), because the picture has to be large enough
to read. No margin can hold a card that wide, so on these steps the card stands over the page,
wherever it covers least of what is lit. When it fits neither above nor below the button it
describes, it narrows to the space beside it rather than cover the button, down to a floor.

Passive Radar takes three steps over one picture, because the display needs explaining rather
than naming: what the white dots are, what the orange dots are, and what it means when they do
not line up. Each step points at dots in the picture. An image's `marks` are points in the
image's own pixels, and `drawMarks` in `tutorial.js` draws a ring and an arrow on each in an SVG
laid over the image with the image's size as its `viewBox`, so a mark stays on its dot whatever
size the picture is shown at. Resizing or replacing the file means measuring the marks again;
`test_every_mark_is_on_its_image` only catches one that has fallen off the edge.

The four service captures are JPEGs, stored at about twice the size they are shown so they stay
sharp on a high-density screen. Three of them are fields of noise, which PNG stores at several
times the size for no visible gain. The two Signal peak captures are the opposite case, flat
colour and small text, so they are PNGs, at the size they were captured.

This repository is public. The radar, Max-Hold and Tracker displays plot range against Doppler
and carry no location, and the Signal peak captures show only the meter. The ADS-B map is the one example image that can show where a node is, so
anything that replaces it should be chosen with that in mind.

## The overlay

`tutorial.js` appends one fixed, full-window element above everything else on the page. It holds
an SVG and the card.

- **Dimming and lit regions.** The SVG is a dim rectangle with a mask; each lit region is a
  rounded hole in the mask with a bright, slowly pulsing ring around it. A mask rather than one
  path with holes, because two regions can overlap (a side-nav link sits inside the side nav) and
  overlapping holes in a single path cancel out.
- **The highlight stays on its target.** A region extends a few pixels past its elements so the
  ring does not sit on their edge, but never further than half the gap to a neighbouring element
  (`paddingFor`), so lighting one side-nav link or one button does not spill onto the next.
- **Measured against the overlay, not the window.** Every position is taken relative to the
  overlay's own size. `window.innerWidth` includes the scrollbar and page coordinates do not, so
  using it scales the whole drawing by a percent or so and the highlight drifts off its target
  towards the right and bottom edges. Browsers that draw overlay scrollbars hide this, which is
  how it got past the first round of testing; test with classic scrollbars.
- **Nothing underneath is clickable.** The overlay takes every click, including on the lit
  regions. The tour points at Restart services, the mode switch and Apply changes, and an owner
  reading a caption should not be able to trigger them by reaching for the page. The two
  [click-through steps](#click-through-steps) are the exception, and even there the overlay
  handles the click itself.
- **Card placement.** Except on steps with a picture, the card stands in the empty margin beside the page's column, centred on
  the region it describes and on whichever side is nearer to it, so nothing on the page itself is
  covered. `margins` measures the space either side of `.shell` (Configuration) or `main.main`
  (Summary and Home), and the card narrows to fit it, down to a floor below which it would be
  unreadable. Only when the window is too narrow for that (a small laptop on the Configuration
  page, a phone anywhere) does `placeCard` fall back to standing the card over the page: it tries
  the spots beside the region, then a grid across the window, and takes whichever covers the
  least of what is lit.
- **Scrolling.** On each step the page scrolls to bring the lit regions into view: centred when
  they fit, top-aligned under the banner when they are taller than the window. In the narrow
  fallback it also leaves room for the card: region then card when both fit, the region at the
  bottom when only it fits. Regions that stay put while the page scrolls (the banner, the
  Configuration side nav, the save bar) are ignored when deciding where to scroll. The page can
  still be scrolled by hand during a step.
- **The card waits for the scroll.** Moving to a step on the same page glides the page there.
  While it glides the card and arrows are hidden (`holdUntilStill`: the `tut-moving` class, until
  no scroll event has arrived for 140 ms), then fade in where they belong. Laid out mid-glide
  they were drawn for wherever the region happened to be and jumped when it stopped, and a
  picture card, which stands over the page instead of in the margin, could land somewhere else
  entirely for a few frames. The rings are not hidden: they only follow their region.
- **Arrows.** One per region that wants one. An arrow ends at the middle of whichever edge of the
  region faces the card, never at a corner, and starts from the card's facing edge as nearly
  opposite that point as the card allows, so with the card level with its region the arrow runs
  straight across. An arrow shorter than a couple of dozen pixels is left out as clutter.
- **A target that is not there.** Some targets depend on the node's state: the Services cards
  are replaced in Spectrum and SDRconnect modes, and the "Listening on" row needs a chosen
  tower. When a step has nothing on the page to light, the card is centred, everything is
  dimmed, and the card says so rather than pointing at empty space.
- **Keyboard.** Right arrow is Next (except on a click-through step), left arrow is Previous,
  Escape is Skip tutorial, and Tab cycles within the card. Focus moves to Next on every step, or to the card itself on a
  click-through step, which has no Next.
- **Keeping up with the page.** The layout is redone on scroll, on resize, and whenever the
  page's height changes (a `ResizeObserver` on `body`), because both pages fill in status rows
  after load and that moves the regions with no scroll or resize event.

Motion is limited to the ring's pulse and smooth scrolling, and both are switched off under
`prefers-reduced-motion`.

## Changing the tour

- To reword, reorder, add or remove a step, edit `STEPS` in `src/tutorial.py`.
- To point at something new, add `data-tutorial="<name>"` to the element in its template and
  name it in the step's targets.
- Update the table under [Steps](#steps), and run `pytest tests/test_tutorial.py`.
