"""
Utilities for converting Pydantic schemas to form field dicts for Jinja rendering.

Compatible with both Pydantic v1 (Debian Bookworm apt) and v2.
"""
from typing import get_origin, get_args, Union

# Detect Pydantic version
try:
    from pydantic import VERSION
    PYDANTIC_V2 = VERSION.startswith("2.")
except ImportError:
    PYDANTIC_V2 = False


def get_field_type(field_info):
    """Get the type annotation from a field (v1/v2 compatible)."""
    if PYDANTIC_V2:
        return field_info.annotation
    else:
        # Pydantic v1: field_info is a ModelField
        return field_info.outer_type_


def get_field_title(field_info, name):
    """Get field title (v1/v2 compatible)."""
    if PYDANTIC_V2:
        return field_info.title or name
    else:
        return field_info.field_info.title or name


def get_field_description(field_info):
    """Get field description (v1/v2 compatible)."""
    if PYDANTIC_V2:
        return field_info.description
    else:
        return field_info.field_info.description


def get_field_readonly(field_info):
    """Check if field is readonly (v1/v2 compatible)."""
    if PYDANTIC_V2:
        extra = field_info.json_schema_extra
        if extra and isinstance(extra, dict):
            return extra.get('readonly', False)
    else:
        # Pydantic v1: extra kwargs passed to Field() are in field_info.extra
        extra = field_info.field_info.extra
        if extra:
            return extra.get('readonly', False)
    return False


def get_field_input_type(field_info):
    """Map Pydantic field type to HTML input type."""
    annotation = get_field_type(field_info)

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
    """Extract ge/le/gt constraints from field metadata (v1/v2 compatible)."""
    constraints = {}

    if PYDANTIC_V2:
        # Pydantic v2: constraints in metadata list
        for meta in field_info.metadata:
            if hasattr(meta, 'ge') and meta.ge is not None:
                constraints['min'] = meta.ge
            if hasattr(meta, 'gt') and meta.gt is not None:
                constraints['min'] = meta.gt
            if hasattr(meta, 'le') and meta.le is not None:
                constraints['max'] = meta.le
    else:
        # Pydantic v1: constraints directly on field_info.field_info
        fi = field_info.field_info
        if hasattr(fi, 'ge') and fi.ge is not None:
            constraints['min'] = fi.ge
        if hasattr(fi, 'gt') and fi.gt is not None:
            constraints['min'] = fi.gt
        if hasattr(fi, 'le') and fi.le is not None:
            constraints['max'] = fi.le

    # Add step for float fields
    annotation = get_field_type(field_info)
    origin = get_origin(annotation)
    if origin is Union:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            annotation = non_none[0]
    if annotation == float:
        constraints['step'] = 'any'  # Allow any decimal

    return constraints


def get_model_fields(model_class):
    """Get fields dict from model class (v1/v2 compatible)."""
    if PYDANTIC_V2:
        return model_class.model_fields
    else:
        return model_class.__fields__


def is_nested_model(annotation):
    """Check if annotation is a nested Pydantic model (v1/v2 compatible)."""
    if PYDANTIC_V2:
        return hasattr(annotation, 'model_fields')
    else:
        return hasattr(annotation, '__fields__')


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
    for name, field_info in get_model_fields(model_class).items():
        annotation = get_field_type(field_info)

        # Handle Optional[X]
        origin = get_origin(annotation)
        if origin is Union:
            args = get_args(annotation)
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                annotation = non_none[0]

        # Check if this is a nested Pydantic model
        if is_nested_model(annotation):
            nested_values = values.get(name) or {}
            fields.append({
                'name': name,
                'title': get_field_title(field_info, name),
                'type': 'group',
                'fields': schema_to_form_fields(annotation, nested_values)
            })
        else:
            constraints = get_field_constraints(field_info)
            readonly = get_field_readonly(field_info)
            fields.append({
                'name': name,
                'title': get_field_title(field_info, name),
                'description': get_field_description(field_info),
                'type': get_field_input_type(field_info),
                'value': values.get(name),  # From user.yml, NOT schema default
                'readonly': readonly,
                **constraints,
            })
    return fields
