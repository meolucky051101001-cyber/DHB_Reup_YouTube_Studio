"""Provider-free Studio metadata contracts; no production DOM adapter is supplied.

Adapters supply scoped observations and read-only saved-video observations. This
module never publishes, deletes drafts, opens Profiles, or guesses selectors.
Synthetic adapters belong in tests and do not establish live Studio support.
"""
from dataclasses import dataclass
import re
from typing import Callable, Mapping, Optional, Protocol, Tuple


class StudioMetadataError(RuntimeError):
    def __init__(self, code, *, after_send=False):
        self.code = code
        self.after_send = after_send
        super().__init__(code)


@dataclass(frozen=True)
class CategoryOption:
    value: str
    enabled: bool = True


@dataclass(frozen=True)
class CategoryControl:
    locale: str
    section_key: str
    scope_matches: int
    control_matches: int
    visible: bool
    enabled: bool
    value: str
    options: Tuple[CategoryOption, ...]


@dataclass(frozen=True)
class SavedCategory:
    video_id: str
    locale: str
    value: str


@dataclass(frozen=True)
class PreparedCategory:
    category_id: str
    locale: str
    value: str
    provenance: str


@dataclass(frozen=True)
class LanguageOption(CategoryOption):
    pass


@dataclass(frozen=True)
class LanguageControl(CategoryControl):
    """A scoped spoken-video-language control, never title or caption metadata."""


@dataclass(frozen=True)
class SavedLanguage(SavedCategory):
    pass


@dataclass(frozen=True)
class PreparedLanguage:
    video_language: str
    locale: str
    value: str
    provenance: str


class CategoryAdapter(Protocol):
    def reveal_category(self) -> None: ...
    def read_category(self) -> CategoryControl: ...
    def select_category(self, value: str) -> None: ...
    def read_saved_category(self, video_id: str) -> SavedCategory: ...


class LanguageAdapter(Protocol):
    def reveal_video_language(self) -> None: ...
    def read_video_language(self) -> LanguageControl: ...
    def select_video_language(self, value: str) -> None: ...
    def read_saved_video_language(self, video_id: str) -> SavedLanguage: ...


class _StudioScopedContract:
    """Shared validation for one semantically identified, explicitly scoped field."""
    def __init__(self, adapter, locale_maps, *, provenance):
        if adapter is None:
            self._fail('ADAPTER_UNAVAILABLE')
        if (not isinstance(provenance, str) or not provenance.strip()
                or not isinstance(locale_maps, Mapping) or not locale_maps):
            self._fail('MAPPING_UNAVAILABLE')
        self._maps = {}
        for locale, values in locale_maps.items():
            if not isinstance(locale, str) or not locale or not isinstance(values, Mapping) or not values:
                self._fail('MAPPING_INVALID')
            mapping = dict(values)
            if any(not self._valid_identifier(key) or not isinstance(value, str)
                   or not value or len(value) > 100 for key, value in mapping.items()):
                self._fail('MAPPING_INVALID')
            if len(set(mapping.values())) != len(mapping):
                self._fail('MAPPING_AMBIGUOUS')
            self._maps[locale] = mapping
        self._adapter, self._provenance = adapter, provenance

    def _fail(self, suffix, *, after_send=False):
        raise StudioMetadataError(f'{self._prefix}_{suffix}', after_send=after_send)

    def _valid_identifier(self, value):
        return isinstance(value, str) and len(value) <= 64 and re.fullmatch(self._pattern, value) is not None

    def _call(self, operation, *args, error, after_send=False):
        try:
            return getattr(self._adapter, f'{operation}_{self._scope}')(*args)
        except Exception:
            raise StudioMetadataError(f'{self._prefix}_{error}', after_send=after_send) from None

    def _read(self):
        control = self._call('read', error='CONTROL_UNAVAILABLE')
        if (type(control) is not self._control_type
                or type(control.scope_matches) is not int or type(control.control_matches) is not int
                or not isinstance(control.section_key, str) or not isinstance(control.locale, str)
                or not isinstance(control.value, str) or type(control.visible) is not bool
                or type(control.enabled) is not bool):
            self._fail('CONTROL_INVALID')
        if control.scope_matches != 1 or control.control_matches != 1 or control.section_key != self._scope:
            self._fail('SCOPE_AMBIGUOUS')
        if control.locale not in self._maps:
            self._fail('LOCALE_UNSUPPORTED')
        if control.visible is not True or control.enabled is not True:
            self._fail('CONTROL_DISABLED')
        if not isinstance(control.options, tuple) or any(
                type(item) is not self._option_type or not isinstance(item.value, str)
                or type(item.enabled) is not bool for item in control.options):
            self._fail('OPTIONS_INVALID')
        return control

    def _target(self, identifier, control):
        value = self._maps[control.locale].get(identifier)
        if value is None:
            self._fail('OPTION_UNMAPPED')
        matches = [item for item in control.options if item.value == value]
        if len(matches) != 1:
            self._fail('OPTION_AMBIGUOUS')
        if matches[0].enabled is not True:
            self._fail('OPTION_DISABLED')
        return value

    def _prepare(self, identifier):
        if not self._valid_identifier(identifier):
            self._fail(self._identifier_error)
        if self._adapter is None:
            self._fail('ADAPTER_UNAVAILABLE')
        self._call('reveal', error='REVEAL_FAILED')
        control = self._read()
        value = self._target(identifier, control)
        if control.value != value:
            self._call('select', value, error='SELECT_FAILED')
        selected = self._read()
        if selected.locale != control.locale or self._target(identifier, selected) != value or selected.value != value:
            self._fail('SET_MISMATCH')
        return self._prepared_type(identifier, control.locale, value, self._provenance)

    def _validate_prepared(self, prepared):
        if (type(prepared) is not self._prepared_type
                or any(not isinstance(value, str) for value in (
                    getattr(prepared, self._field), prepared.locale, prepared.value, prepared.provenance))
                or prepared.provenance != self._provenance
                or self._maps.get(prepared.locale, {}).get(getattr(prepared, self._field)) != prepared.value):
            self._fail('PREPARATION_INVALID')

    def recheck(self, prepared, *, verify_binding: Callable[[], None], verify_approval: Callable[[], None]):
        """Call immediately before the caller's save/publish side effect."""
        self._validate_prepared(prepared)
        if not callable(verify_binding) or not callable(verify_approval):
            self._fail('GUARD_REQUIRED')
        for callback, suffix in ((verify_binding, 'BINDING_CHANGED'), (verify_approval, 'APPROVAL_STALE')):
            try:
                if callback() is False:
                    raise ValueError()
            except Exception:
                raise StudioMetadataError(f'{self._prefix}_{suffix}') from None
        control = self._read()
        if (control.locale != prepared.locale
                or self._target(getattr(prepared, self._field), control) != prepared.value
                or control.value != prepared.value):
            self._fail('CHANGED_BEFORE_PUBLISH')

    def _read_saved(self, prepared, video_id):
        saved = self._call('read_saved', video_id, error='READBACK_FAILED', after_send=True)
        if (type(saved) is not self._saved_type or any(not isinstance(value, str)
                for value in (saved.video_id, saved.locale, saved.value))):
            self._fail('READBACK_INVALID', after_send=True)
        if saved.video_id != video_id:
            self._fail('READBACK_ID_MISMATCH', after_send=True)
        if saved.locale != prepared.locale:
            self._fail('READBACK_LOCALE_CHANGED', after_send=True)
        reverse = {value: identifier for identifier, value in self._maps[saved.locale].items()}
        identifier = reverse.get(saved.value)
        if identifier is None:
            self._fail('READBACK_UNMAPPED', after_send=True)
        return saved, identifier


class StudioCategoryContract(_StudioScopedContract):
    """Category ID to observed semantic values; preserves the G20 category API."""
    _prefix, _scope, _field = 'STUDIO_CATEGORY', 'category', 'category_id'
    _pattern, _identifier_error = r'[1-9][0-9]{0,2}', 'ID_INVALID'
    _control_type, _option_type = CategoryControl, CategoryOption
    _saved_type, _prepared_type = SavedCategory, PreparedCategory

    def prepare(self, category_id: str) -> PreparedCategory:
        return self._prepare(category_id)

    def verify_saved(self, prepared: PreparedCategory, video_id: str, *, evidence_sink: Callable) -> SavedCategory:
        return verify_saved_metadata(video_id, category=(self, prepared), evidence_sink=evidence_sink)['category_id']


class StudioLanguageContract(_StudioScopedContract):
    """Spoken video language only; tags and region subtags match exactly."""
    _prefix, _scope, _field = 'STUDIO_LANGUAGE', 'video_language', 'video_language'
    _pattern, _identifier_error = r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', 'TAG_INVALID'
    _control_type, _option_type = LanguageControl, LanguageOption
    _saved_type, _prepared_type = SavedLanguage, PreparedLanguage

    def __init__(self, adapter: Optional[LanguageAdapter], locale_maps=None, *, provenance=''):
        # An absent adapter is usable only for the explicit no-op keep path.
        if adapter is None:
            self._adapter, self._maps, self._provenance = None, {}, provenance
        else:
            super().__init__(adapter, locale_maps, provenance=provenance)

    def prepare(self, video_language=None, *, mode='apply') -> Optional[PreparedLanguage]:
        if mode == 'keep':
            return None
        if mode != 'apply':
            self._fail('MODE_INVALID')
        return self._prepare(video_language)

    def recheck(self, prepared, *, verify_binding=None, verify_approval=None):
        if prepared is None:
            return None
        return super().recheck(prepared, verify_binding=verify_binding, verify_approval=verify_approval)

    def verify_saved(self, prepared, video_id, *, evidence_sink):
        if prepared is None:
            return None
        return verify_saved_metadata(video_id, language=(self, prepared), evidence_sink=evidence_sink)['video_language']


def verify_saved_metadata(video_id, *, category=None, language=None, evidence_sink):
    """Persist one receipt and one aggregate result for all requested fields.

    Each field argument is ``(contract, prepared)`` or None (keep/not requested).
    Every requested field is read using the same already-observed video ID before
    verified is possible. Failures after receipt are uncertain: the caller must
    retain its reservation and use NeedsReview, never retry or delete a draft.
    With no requested fields this function performs no adapter or sink I/O.
    """
    pairs = []
    for pair, required_type in ((category, StudioCategoryContract), (language, StudioLanguageContract)):
        if pair is None:
            continue
        if not isinstance(pair, tuple) or len(pair) != 2 or not isinstance(pair[0], required_type):
            raise StudioMetadataError('STUDIO_METADATA_PREPARATION_INVALID', after_send=True)
        contract, prepared = pair
        try:
            contract._validate_prepared(prepared)
        except StudioMetadataError as exc:
            raise StudioMetadataError(exc.code, after_send=True) from None
        pairs.append((contract, prepared))
    if not pairs:
        return {}
    prefix = pairs[0][0]._prefix if len(pairs) == 1 else 'STUDIO_METADATA'
    if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise StudioMetadataError(f'{prefix}_VIDEO_ID_INVALID', after_send=True)
    if not callable(evidence_sink):
        raise StudioMetadataError(f'{prefix}_EVIDENCE_REQUIRED', after_send=True)
    expected = {contract._field: getattr(prepared, contract._field) for contract, prepared in pairs}

    def record(state, code, observed=None):
        try:
            evidence_sink({'backend': 'playwright', 'video_id': video_id, 'expected': expected.copy(),
                           'verification_state': state, 'code': code, 'observed': observed or {}})
        except Exception:
            raise StudioMetadataError(f'{prefix}_EVIDENCE_FAILED', after_send=True) from None

    record('received', f'{prefix}_ID_RECEIVED')
    observations, results, errors = {}, {}, []
    for contract, prepared in pairs:
        try:
            saved, identifier = contract._read_saved(prepared, video_id)
        except StudioMetadataError as exc:
            errors.append(('unknown', exc.code))
            continue
        observations[contract._field], results[contract._field] = identifier, saved
        if identifier != expected[contract._field]:
            errors.append(('mismatch', f'{contract._prefix}_READBACK_MISMATCH'))
    if errors:
        state, code = next((error for error in errors if error[0] == 'unknown'), errors[0])
        record(state, code, observations)
        raise StudioMetadataError(code, after_send=True)
    record('verified', f'{prefix}_VERIFIED', observations)
    return results
