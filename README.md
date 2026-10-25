# Walhad Housing Solutions

A responsive, deployment-ready real-estate website for **Walhad Housing Solutions**, serving Borama, Hargeisa, and Gabiley.

The application uses Flask with all backend logic, HTML, CSS, and JavaScript embedded in one file: `app.py`. There is no frontend build step and no database in this initial version.

## Included features

- Sticky desktop navigation and accessible mobile menu
- Two-slide hero carousel that changes every 10 seconds
- Reduced-motion support (automatic rotation is disabled for users who request reduced motion)
- Uploaded Walhad logo and Mohamed Said Ahmed portrait
- Two supplied property photographs, cropped from the uploaded listing screenshots
- Working location, purpose, property-type, bedroom, and maximum-price filters
- Hero search connected to the property filters
- Direct-linkable property detail dialog
- Responsive card layouts and graceful missing-image fallback
- Server-side and client-side contact validation
- Session-bound CSRF protection, a honeypot, request-size limits, and basic rate limiting
- Honest contact-form demo mode when no delivery service is configured
- Optional secure HTTPS webhook integration for real inquiry delivery
- Custom 404/500 pages and a `/healthz` endpoint
- Render Blueprint configuration and Gunicorn support

## Project structure

```text
walhad-housing-solutions/
├── app.py
├── requirements.txt
├── render.yaml
├── README.md
├── .gitignore
└── static/
    └── images/
        ├── walhad-logo.jpg
        ├── mohamed-said-ahmed.jpg
        ├── hodan-hills-3-bedroom.jpg
        └── hodan-hills-5-bedroom.jpg
```

The application remains a single Python file. Images are normal static assets so GitHub and Render can deploy the exact supplied media reliably.

## Run locally

Python 3.10 or newer is recommended.

```bash
cd walhad-housing-solutions
python -m venv .venv
```

Activate the virtual environment:

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install and run:

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

For a production-style local test:

```bash
gunicorn app:app
```

Gunicorn is intended for Linux/macOS and the Render deployment environment. On Windows, use `python app.py` for local development.

## Edit properties

Open the clearly marked `PROPERTIES` section near the top of `app.py`. Each listing uses this structure:

```python
{
    "id": "unique-url-safe-id",
    "reference": "WHS-HRG-003",
    "title": "Property title",
    "location": "Hargeisa",       # Borama, Hargeisa, or Gabiley
    "area": "Area supplied by owner",
    "purpose": "rent",            # rent or sale
    "property_type": "House",     # House, Apartment, Villa, Commercial
    "price": None,                 # None displays “Price on Request”
    "currency": "USD",
    "price_period": "month",      # or None for a sale price
    "bedrooms": 3,
    "bathrooms": 2,
    "description": "Confirmed description",
    "features": ["Confirmed feature"],
    "images": ["new-property.jpg"],
    "availability": "Contact to confirm",
    "is_sample": False,
}
```

Place new image files in `static/images/`, then list their filenames in the property record. Use factual, owner-confirmed information only. If a price is unknown, set `price` to `None`; the interface will display **Price on Request**.

The current uploaded-image mapping is:

| Asset | Website use |
|---|---|
| `walhad-logo.jpg` | Header, footer, favicon, and error pages |
| `mohamed-said-ahmed.jpg` | Executive hero and About profile |
| `hodan-hills-3-bedroom.jpg` | WHS-HRG-001 card, details, and first hero |
| `hodan-hills-5-bedroom.jpg` | WHS-HRG-002 card, details, and About section |

The two property assets were cropped only to remove the old listing interface and price text surrounding the photographs. The supplied property details retained in `app.py` are limited to information visible in those source listings. Confirm price, availability, and all details before publication.

## Contact settings and form behavior

Without `CONTACT_WEBHOOK_URL`, the form runs in **demo mode**. It validates the inquiry but explicitly tells the visitor that no message was sent. This avoids a false delivery claim.

Supported environment variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Required in production; signs Flask sessions and CSRF state |
| `SESSION_COOKIE_SECURE` | Set to `true` on HTTPS deployments |
| `CONTACT_EMAIL` | Displayed business email |
| `CONTACT_PHONE` | Displayed business phone |
| `WHATSAPP_NUMBER` | Verified WhatsApp number in international format, e.g. `252...` |
| `OFFICE_ADDRESS` | Optional confirmed office address |
| `FACEBOOK_URL` | Optional official Facebook HTTPS URL |
| `INSTAGRAM_URL` | Optional official Instagram HTTPS URL |
| `LINKEDIN_URL` | Optional official LinkedIn HTTPS URL |
| `CONTACT_WEBHOOK_URL` | HTTPS endpoint that receives validated inquiries as JSON |
| `CONTACT_WEBHOOK_TOKEN` | Optional bearer token sent only to the configured webhook |
| `PORT` | Local server port; defaults to `5000` |

When configured, the webhook receives:

```json
{
  "full_name": "...",
  "email": "...",
  "phone": "...",
  "location": "Hargeisa",
  "inquiry_type": "Rent",
  "property_reference": "WHS-HRG-001",
  "message": "...",
  "submitted_at": "ISO-8601 UTC timestamp",
  "source": "walhad-housing-solutions-website"
}
```

Use a trusted form backend, automation service, or your own HTTPS API. Keep tokens only in environment variables. Do not commit secrets to GitHub.

> The built-in rate limiter is in-memory and suitable as a lightweight first layer. A multi-instance, high-traffic production service should use a shared rate-limit store such as Redis and a persistent CRM or database.

## Upload to GitHub

Create an empty GitHub repository, then run from this project folder:

```bash
git init
git add .
git commit -m "Launch Walhad Housing Solutions website"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
git push -u origin main
```

Before pushing, confirm that all four files in `static/images/` are tracked:

```bash
git status
git ls-files static/images
```

## Deploy on Render using `render.yaml`

1. Push the complete project to GitHub.
2. In Render, choose **New → Blueprint**.
3. Connect the GitHub repository.
4. Render will read `render.yaml` and create the web service.
5. Enter the confirmed contact values for environment variables marked `sync: false`.
6. Add a secure `CONTACT_WEBHOOK_URL` and optional token when real form delivery is ready.
7. Deploy, then verify `/healthz`, the property filters, property details, and a contact test.

`SECRET_KEY` is generated by Render from the Blueprint and should never be copied into the repository.

## Deploy on Render manually

If you prefer not to use the Blueprint:

1. Choose **New → Web Service** and connect the GitHub repository.
2. Select the Python runtime.
3. Set the build command to:

   ```text
   pip install -r requirements.txt
   ```

4. Set the start command to:

   ```text
   gunicorn app:app
   ```

5. Set the health check path to `/healthz`.
6. Add `SECRET_KEY` with a long random value, plus any confirmed contact variables.
7. Deploy. Render provides `PORT`; `app.py` also honors it when run directly.

## Image deployment notes

Files uploaded only to a temporary chat or local machine do not automatically appear on a deployed website. This project already copies the selected uploaded media into `static/images/`; commit that directory to GitHub so Render receives it.

When adding future photos:

1. Export/download the image.
2. Give it a simple lowercase filename without spaces.
3. Place it in `static/images/`.
4. Reference that filename in the relevant `PROPERTIES` record.
5. Commit and push both the code and image.

For good performance, use properly licensed JPG/WebP images, remove private metadata where appropriate, and keep each optimized image reasonably sized.

## Persistence limitation

Property data currently lives in Python memory and changes require a code deployment. The contact form does not save submissions locally. Render’s service filesystem is ephemeral: files created while the service is running may disappear on restart, redeploy, or instance replacement.

For a production property-management system, use persistent external services such as:

- PostgreSQL for listings and availability
- Object storage for uploaded property images
- A CRM, email provider, or protected webhook for inquiries
- Redis or another shared store for cross-instance rate limiting

Do not use temporary files on Render as permanent listing, image, or inquiry storage.

## Final launch checklist

- Replace “Not yet provided” contact fields with verified company details.
- Configure and test the inquiry webhook, or keep the clear demo-mode disclosure.
- Confirm both supplied prices and current availability.
- Add only verified features, addresses, and ownership details.
- Test navigation and filters on desktop and mobile.
- Confirm the two hero slides rotate after 10 seconds.
- Test keyboard navigation, the property dialog, and form errors.
- Confirm the logo and all property images load from the deployed GitHub files.
- Keep `SECRET_KEY` and webhook tokens out of source control.
