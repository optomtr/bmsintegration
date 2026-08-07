# Brand assets for home-assistant/brands

Home Assistant and HACS do **not** read icons from this repository. They are
served from https://brands.home-assistant.io, which is built from the
[home-assistant/brands](https://github.com/home-assistant/brands) repository.
Until the domain is present there, HACS shows the "Icon not available"
placeholder, and the integration page shows a generic puzzle icon.

`custom_integrations/bms_integration/` in this folder is the exact payload to
submit. The images already meet the specification:

| File          | Size      | Requirement            |
| ------------- | --------- | ---------------------- |
| `icon.png`    | 256x256   | square, exactly 256x256 |
| `icon@2x.png` | 512x512   | square, exactly 512x512 |
| `logo.png`    | 512x198   | max 512 wide, 256 high  |
| `logo@2x.png` | 1024x396  | twice the logo.png size |

## How to submit

1. Fork https://github.com/home-assistant/brands
2. Copy `custom_integrations/bms_integration/` into the fork, keeping the path:

   ```text
   custom_integrations/bms_integration/icon.png
   custom_integrations/bms_integration/icon@2x.png
   custom_integrations/bms_integration/logo.png
   custom_integrations/bms_integration/logo@2x.png
   ```

3. Open a pull request titled `Add BMS Integration`.
4. In the description, link this repository
   (https://github.com/optomtr/bmsintegration) - reviewers check that the
   domain in `manifest.json` (`bms_integration`) matches the folder name.

Review usually takes a few days. Once merged, the icon appears in HACS and in
Settings -> Devices & services without any change on this side; the CDN cache
may take a few hours.

Notes:
- The folder name must be exactly the integration domain: `bms_integration`.
- Images should be trimmed (no empty margin) and look correct on both the
  light and the dark theme - they are shown on both.
- `logo*.png` is optional; `icon*.png` alone is enough to remove the
  placeholder.
