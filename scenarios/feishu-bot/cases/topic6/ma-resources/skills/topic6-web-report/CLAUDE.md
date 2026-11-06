# BlueFocus hotspot-report requirements

Before generating or updating a report, read `SKILL.md` and `references/document-image-extraction.md` completely and follow them as mandatory project requirements.

Before copying the retained template, run `scripts/validate-template-assets.mjs` from the skill directory. Do not build when a retained PNG, JPEG, or TTF asset fails validation.

For Feishu/Lark sources, reading `rawContent` alone is not sufficient. Also retrieve the complete document blocks, extract each image token from its source block, download the original image binary through the authenticated Feishu/Lark connector or API, and map it back to the same `docx-grid-block` in `source.json`.

Never generate, redraw, search for, or substitute marketing-observation case images. Never reuse the retained W32 `case-*.jpg` assets for a new report. If original document images cannot be retrieved, stop and ask the user for a DOCX export or the original image files.

Run the bundled validator before building:

```bash
node scripts/validate-case-image-sources.mjs /absolute/path/source.json
```

Do not publish output containing `blob:` image URLs.

Do not upload an HTML snapshot. In Topic6 the only web delivery is the Miaoda (妙搭) publish in Phase H; never run `scripts/upload-html.mjs` (including `--dry-run` / `--setup-key`) and never ask the user to set up a key. Return the validated local `index.html`. This workflow does not ask about or perform Sites creation, modification, or deletion.
