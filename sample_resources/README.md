# sample_resources

A couple of realistic "student notes" files so the app can be demoed
immediately: the easiest way is the Resource List's file picker, which
uploads whatever you choose (single files or a whole folder). This
directory is also mounted read-only at `/data/sample_resources` inside the
containers, so the files can alternatively be registered by that path via
the API (a directory path works too, expanding to one item per file).
The classifier should tag
them `personal-notes` with a lecture/date guess; correct it if it's wrong —
that's the point of editable tags.

For a real demo, also add the course schedule URL
(https://aicoding.cs.wisc.edu/fa26/schedule.html) and watch the crawler
register the readings/videos/specs it links to.
