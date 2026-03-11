from django import forms
from django.contrib.auth.models import User
from django.core.validators import FileExtensionValidator

from scanning.models import (
    OpinionScan,
    OpinionStatus,
    Reporter,
    Scan,
    Status,
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

    def label_from_instance(self, obj):
        """Format the display label for a Reporter instance.

        :param obj: The Reporter instance.
        :type obj: Reporter
        :returns: The formatted label.
        :rtype: str
        """
        return f"{obj.full_name} ({obj.short_name})"


class ScanUploadForm(forms.ModelForm):
    """Form for uploading a new scan."""

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

    class Meta:
        model = Scan
        fields = [
            "reporter",
            "source",
            "volume",
            "number_of_pages",
            "start_page",
            "end_page",
            "book_cover",
            "original_pdf",
            "notes",
        ]
        widgets = {
            "source": forms.Select(attrs={"class": "input-text w-full"}),
            "volume": forms.NumberInput(attrs={"class": "input-text w-full"}),
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
                attrs={
                    "class": "input-text w-full",
                    "accept": ".pdf,.jpg,.jpeg,.gif,.png",
                }
            ),
            "notes": forms.Textarea(
                attrs={
                    "class": "input-text w-full",
                    "rows": 3,
                }
            ),
        }

    def clean_original_pdf(self):
        """Validate that the uploaded file is a PDF by MIME type and magic bytes.

        :returns: The validated file.
        :rtype: django.core.files.uploadedfile.UploadedFile
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


class OpinionScanReviewForm(forms.ModelForm):
    """Form for staff to review an opinion scan."""

    status = forms.ChoiceField(
        choices=[
            (OpinionStatus.OK, "Approve"),
            (OpinionStatus.NO_STATUS, "Reject (reset to No status)"),
        ],
        widget=forms.Select(attrs={"class": "input-text w-full"}),
    )

    class Meta:
        model = OpinionScan
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
    masked_pdf = forms.FileField(
        required=False,
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
            "masked_pdf",
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

    def clean_original_pdf(self):
        """Validate that the uploaded file is a PDF by MIME type and magic bytes.

        :returns: The validated file.
        :rtype: django.core.files.uploadedfile.UploadedFile
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
