Docker mounts this empty folder in place of your Google sign-in until you set
`GCLOUD_CONFIG_DIR` in `.env`. Mock mode and the tests don't need a sign-in.
For real Gemini calls, follow https://handouts-1a578e.pages.doit.wisc.edu/gemini-api-setup.html
