from datetime import timedelta

from django import forms

from apps.customers.models import Address, Customer
from apps.depots.models import Depot
from apps.orders.models import Order


class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            "external_ref",
            "depot",
            "customer",
            "order_type",
            "priority",
            "pickup_address",
            "delivery_address",
            "service_date",
            "time_window_start",
            "time_window_end",
            "service_duration_seconds",
            "demand_weight_kg",
            "demand_volume_m3",
            "package_count",
            "special_instructions",
        ]
        widgets = {
            "service_date": forms.DateInput(attrs={"type": "date", "class": "field"}),
            "time_window_start": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "field"}),
            "time_window_end": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "field"}),
            "special_instructions": forms.Textarea(attrs={"rows": 3, "class": "field"}),
        }

    def __init__(self, *args, organization=None, allowed_depots=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Widget classes are set here rather than only in Meta.widgets so the
        # select and text inputs pick up the shared `.field` treatment too.
        # Without this the create-order form fell back to the bare
        # `form p input` rule and looked unlike every other form in the app.
        for field in self.fields.values():
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"field {existing}".strip()
        if organization:
            self.fields["depot"].queryset = (
                allowed_depots
                if allowed_depots is not None
                else organization.depots.filter(active=True)
            )
            self.fields["customer"].queryset = Customer.objects.filter(organization=organization)
            address_queryset = Address.objects.filter(organization=organization)
            self.fields["pickup_address"].queryset = address_queryset
            self.fields["delivery_address"].queryset = address_queryset


class ReportExportForm(forms.Form):
    """Builds a KPI export from the operations console.

    The reports page previously offered no way to create an export at all â€” its
    only call to action was a link into the Swagger UI, which cannot express a
    date range or a depot. The report *type* is deliberately not offered as a
    choice: ``export_report_task`` produces exactly one KPI summary, so a
    type picker with three options would advertise capability that does not
    exist. The type is fixed here and the form covers the filters the exporter
    actually honours.
    """

    REPORT_TYPE = "kpi_summary"

    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field"}),
        help_text="Defaults to the start of the current service week.",
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "field"}),
        help_text="Defaults to today.",
    )
    depot = forms.ModelChoiceField(
        required=False,
        queryset=Depot.objects.none(),
        empty_label="All depots in my scope",
        widget=forms.Select(attrs={"class": "field"}),
    )

    def __init__(self, *args, allowed_depots=None, today=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["depot"].queryset = (
            allowed_depots if allowed_depots is not None else Depot.objects.none()
        )
        self.today = today
        for field in self.fields.values():
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"field {existing}".strip()

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        today = self.today
        if date_from and date_to and date_to < date_from:
            self.add_error("date_to", "The end date cannot be before the start date.")
        if date_to and today and date_to > today:
            self.add_error("date_to", "The end date cannot be in the future.")
        return cleaned

    def build_filters(self):
        """Resolve the form into the ``filters_json`` the exporter consumes."""
        today = self.today
        data = self.cleaned_data
        filters = {}
        if data.get("date_from"):
            filters["date_from"] = data["date_from"].isoformat()
        else:
            filters["date_from"] = (today - timedelta(days=today.weekday())).isoformat()
        if data.get("date_to"):
            filters["date_to"] = data["date_to"].isoformat()
        else:
            filters["date_to"] = today.isoformat()
        if data.get("depot"):
            filters["depot_id"] = data["depot"].pk
        return filters
