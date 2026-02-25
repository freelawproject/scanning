from django import forms
from django.core.validators import FileExtensionValidator

from scanning.models import Scan, Status


class ScanUploadForm(forms.ModelForm):
    """Form for uploading a new scan."""

    original_pdf = forms.FileField(
        validators=[FileExtensionValidator(allowed_extensions=["pdf"])],
        widget=forms.ClearableFileInput(
            attrs={"class": "input-text w-full", "accept": ".pdf"}
        ),
    )

    class Meta:
        model = Scan
        fields = [
            "reporter",
            "volume",
            "number_of_pages",
            "start_page",
            "end_page",
            "book_cover",
            "original_pdf",
            "notes",
        ]
        widgets = {
            "reporter": forms.Select(attrs={"class": "input-text w-full"}),
            "volume": forms.NumberInput(
                attrs={"class": "input-text w-full"}
            ),
            "number_of_pages": forms.NumberInput(
                attrs={"class": "input-text w-full"}
            ),
            "start_page": forms.NumberInput(
                attrs={"class": "input-text w-full"}
            ),
            "end_page": forms.NumberInput(
                attrs={"class": "input-text w-full"}
            ),
            "book_cover": forms.ClearableFileInput(
                attrs={"class": "input-text w-full", "accept": "image/*"}
            ),
            "notes": forms.Textarea(
                attrs={
                    "class": "input-text w-full",
                    "rows": 3,
                }
            ),
        }


class ScanReviewForm(forms.ModelForm):
    """Form for staff to review a scan."""

    status = forms.ChoiceField(
        choices=[
            (Status.APPROVED, "Approve"),
            (Status.UPLOADED, "Reject (reset to Uploaded)"),
        ],
        widget=forms.Select(attrs={"class": "input-text w-full"}),
    )

    class Meta:
        model = Scan
        fields = ["status", "notes"]
        widgets = {
            "notes": forms.Textarea(
                attrs={
                    "class": "input-text w-full",
                    "rows": 3,
                    "placeholder": "Review notes...",
                }
            ),
        }
