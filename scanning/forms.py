from django import forms
from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import FileExtensionValidator

from scanning.models import (
    OpinionScan,
    Reporter,
)


class ProfileForm(forms.ModelForm):
    """Form for editing user profile information."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]
        widgets = {
            "first_name": forms.TextInput(
                attrs={"class": "input-text w-full"}
            ),
            "last_name": forms.TextInput(attrs={"class": "input-text w-full"}),
            "email": forms.EmailInput(attrs={"class": "input-text w-full"}),
        }


class ReporterChoiceField(forms.ModelChoiceField):
    """ModelChoiceField that displays reporters as 'Full Name (slug)'."""

    def label_from_instance(self, obj: Reporter) -> str:
        """Format the display label for a Reporter instance.

        :param obj: The Reporter instance.
        :return: The formatted label.
        """
        return f"{obj.full_name} ({obj.short_name})"


class OpinionScanUploadForm(forms.ModelForm):
    """Form for uploading a standalone opinion scan."""

    reporter = ReporterChoiceField(
        queryset=Reporter.objects.all(),
        widget=forms.Select(attrs={"class": "input-text w-full"}),
    )
    original_pdf = forms.FileField(
        validators=[FileExtensionValidator(allowed_extensions=["pdf"])],
        widget=forms.ClearableFileInput(
            attrs={"class": "input-text w-full", "accept": ".pdf"}
        ),
    )
    redacted_pdf = forms.FileField(
        required=False,
        validators=[FileExtensionValidator(allowed_extensions=["pdf"])],
        widget=forms.ClearableFileInput(
            attrs={"class": "input-text w-full", "accept": ".pdf"}
        ),
    )

    class Meta:
        model = OpinionScan
        fields = [
            "reporter",
            "volume",
            "original_pdf",
            "redacted_pdf",
            "page_start",
            "page_end",
            "status",
            "notes",
        ]
        widgets = {
            "volume": forms.NumberInput(attrs={"class": "input-text w-full"}),
            "page_start": forms.NumberInput(
                attrs={"class": "input-text w-full"}
            ),
            "page_end": forms.NumberInput(
                attrs={"class": "input-text w-full"}
            ),
            "status": forms.Select(attrs={"class": "input-text w-full"}),
            "notes": forms.Textarea(
                attrs={
                    "class": "input-text w-full",
                    "rows": 3,
                }
            ),
        }

    def clean_original_pdf(self) -> UploadedFile:
        """Validate that the uploaded file is a PDF by MIME type and magic bytes.

        :return: The validated file.
        """
        pdf = self.cleaned_data.get("original_pdf")
        if pdf:
            if pdf.content_type != "application/pdf":
                raise forms.ValidationError("The uploaded file must be a PDF.")
            header = pdf.read(5)
            pdf.seek(0)
            if header != b"%PDF-":
                raise forms.ValidationError("The uploaded file must be a PDF.")
        return pdf
