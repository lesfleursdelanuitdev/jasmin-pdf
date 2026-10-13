"""Import the sample wedding invitation as a published template.

    python -m scripts.seed
"""
from __future__ import annotations

from app.config import ROOT, load_settings
from app.services import Services


def seed(svc: Services) -> dict:
    existing = {t["id"] for t in svc.list_templates()}
    if "wedding-invitation" in existing:
        return svc.get_template("wedding-invitation")
    pdf = (ROOT / "samples" / "wedding-invitation.pdf").read_bytes()
    t = svc.create_template("Arch Wedding Invitation", pdf, template_id="wedding-invitation")
    labels = {
        "couple-photo": {"label": "Your photo", "help": "A portrait photo works best in the arch."},
        "couple-names": {"label": "Your names"},
        "date": {"label": "Date & time"},
        "venue": {"label": "Venue"},
        "story-photo": {
            "label": "Photo on the back",
            "required": False,
            "keepOriginalIfEmpty": True,
            "help": "Optional. Leave empty to keep our illustration.",
        },
        "rsvp": {"label": "RSVP details"},
    }
    changes = [{"id": el["id"], **labels[el["id"]]} for el in t["definition"]["elements"] if el["id"] in labels]
    return svc.update_template(t["id"], {"elements": changes, "status": "published"})


if __name__ == "__main__":
    t = seed(Services(load_settings()))
    print(f"Template '{t['id']}' (revision {t['revision']}, {t['status']}) is ready.")
    print("Open http://localhost:8000/demo-store or http://localhost:8000/editor/wedding-invitation")
