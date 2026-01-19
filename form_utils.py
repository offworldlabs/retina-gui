"""
Utilities for converting Pydantic schemas to form field dicts for Jinja rendering.
"""
from typing import get_origin, get_args, Union


def get_field_input_type(field_info):
    """Map Pydantic field type to HTML input type."""
    annotation = field_info.annotation

    # Handle Optional[X] by extracting X
    origin = get_origin(annotation)
    if origin is Union:
        args = get_args(annotation)
        # Filter out NoneType
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            annotation = non_none[0]

    if annotation == bool:
        return "checkbox"
    elif annotation in (int, float):
        return "number"
    else:
        return "text"


def get_field_constraints(field_info):
    """Extract ge/le/gt constraints from field metadata."""
    constraints = {}
    for meta in field_info.metadata:
        if hasattr(meta, 'ge') and meta.ge is not None:
            constraints['min'] = meta.ge
        if hasattr(meta, 'gt') and meta.gt is not None:
            # gt=0 means min should be just above 0, use small step
            constraints['min'] = meta.gt
        if hasattr(meta, 'le') and meta.le is not None:
            constraints['max'] = meta.le

    # Add step for float fields
    annotation = field_info.annotation
    origin = get_origin(annotation)
    if origin is Union:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            annotation = non_none[0]
    if annotation == float:
        constraints['step'] = 'any'  # Allow any decimal

    return constraints


def schema_to_form_fields(model_class, values: dict):
    """
    Convert Pydantic model to form field dicts for Jinja template.

    Args:
        model_class: Pydantic model class (for field metadata)
        values: Current values from user.yml (what to display in form)

    Returns:
        List of field dicts for Jinja template
    """
    fields = []
    for name, field_info in model_class.model_fields.items():
        annotation = field_info.annotation

        # Handle Optional[X]
        origin = get_origin(annotation)
        if origin is Union:
            args = get_args(annotation)
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                annotation = non_none[0]

        # Check if this is a nested Pydantic model
        if hasattr(annotation, 'model_fields'):
            nested_values = values.get(name) or {}
            fields.append({
                'name': name,
                'title': field_info.title or name,
                'type': 'group',
                'fields': schema_to_form_fields(annotation, nested_values)
            })
        else:
            constraints = get_field_constraints(field_info)
            fields.append({
                'name': name,
                'title': field_info.title or name,
                'description': field_info.description,
                'type': get_field_input_type(field_info),
                'value': values.get(name),  # From user.yml, NOT schema default
                **constraints,
            })
    return fields
