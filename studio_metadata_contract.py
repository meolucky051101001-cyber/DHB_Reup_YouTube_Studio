"""Provider-free Studio category contract; no production DOM adapter is supplied.

Adapters must supply scoped observations and read-only saved-video observations.
This module never publishes, deletes drafts, opens Profiles, or guesses selectors.
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


class CategoryAdapter(Protocol):
    def reveal_category(self) -> None: ...
    def read_category(self) -> CategoryControl: ...
    def select_category(self, value: str) -> None: ...
    def read_saved_category(self, video_id: str) -> SavedCategory: ...


class StudioCategoryContract:
    """Validate observations around external publish/save boundaries.

    ``locale_maps`` maps category IDs to observed semantic option values, never
    display labels. The caller owns provenance review. Passing a test mapping is
    not a mechanism to enable the application's production capability gate.
    """
    def __init__(self, adapter: Optional[CategoryAdapter],
                 locale_maps: Mapping[str, Mapping[str, str]], *, provenance: str):
        if adapter is None:
            raise StudioMetadataError('STUDIO_CATEGORY_ADAPTER_UNAVAILABLE')
        if not isinstance(provenance, str) or not provenance.strip() or not isinstance(locale_maps, Mapping) or not locale_maps:
            raise StudioMetadataError('STUDIO_CATEGORY_MAPPING_UNAVAILABLE')
        self._maps = {}
        for locale, values in locale_maps.items():
            if not isinstance(locale, str) or not locale or not isinstance(values, Mapping) or not values:
                raise StudioMetadataError('STUDIO_CATEGORY_MAPPING_INVALID')
            mapping = dict(values)
            if any(not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{0,2}', key)
                   or not isinstance(value, str) or not value or len(value) > 100
                   for key, value in mapping.items()):
                raise StudioMetadataError('STUDIO_CATEGORY_MAPPING_INVALID')
            if len(set(mapping.values())) != len(mapping):
                raise StudioMetadataError('STUDIO_CATEGORY_MAPPING_AMBIGUOUS')
            self._maps[locale] = mapping
        self._adapter = adapter
        self._provenance = provenance

    def _read(self):
        try:
            control = self._adapter.read_category()
        except Exception:
            raise StudioMetadataError('STUDIO_CATEGORY_CONTROL_UNAVAILABLE') from None
        if not isinstance(control, CategoryControl):
            raise StudioMetadataError('STUDIO_CATEGORY_CONTROL_INVALID')
        if (type(control.scope_matches) is not int or type(control.control_matches) is not int
                or not isinstance(control.section_key, str) or not isinstance(control.locale, str)
                or not isinstance(control.value, str) or type(control.visible) is not bool
                or type(control.enabled) is not bool):
            raise StudioMetadataError('STUDIO_CATEGORY_CONTROL_INVALID')
        if control.scope_matches != 1 or control.control_matches != 1 or control.section_key != 'category':
            raise StudioMetadataError('STUDIO_CATEGORY_SCOPE_AMBIGUOUS')
        if control.locale not in self._maps:
            raise StudioMetadataError('STUDIO_CATEGORY_LOCALE_UNSUPPORTED')
        if control.visible is not True or control.enabled is not True:
            raise StudioMetadataError('STUDIO_CATEGORY_CONTROL_DISABLED')
        if not isinstance(control.options, tuple) or any(
                not isinstance(item, CategoryOption) or not isinstance(item.value, str)
                or type(item.enabled) is not bool for item in control.options):
            raise StudioMetadataError('STUDIO_CATEGORY_OPTIONS_INVALID')
        return control

    def _target(self, category_id, control):
        value = self._maps[control.locale].get(category_id)
        if value is None:
            raise StudioMetadataError('STUDIO_CATEGORY_OPTION_UNMAPPED')
        matches = [item for item in control.options if item.value == value]
        if len(matches) != 1:
            raise StudioMetadataError('STUDIO_CATEGORY_OPTION_AMBIGUOUS')
        if matches[0].enabled is not True:
            raise StudioMetadataError('STUDIO_CATEGORY_OPTION_DISABLED')
        return value

    def prepare(self, category_id: str) -> PreparedCategory:
        if not isinstance(category_id, str) or not re.fullmatch(r'[1-9][0-9]{0,2}', category_id):
            raise StudioMetadataError('STUDIO_CATEGORY_ID_INVALID')
        try:
            self._adapter.reveal_category()
        except Exception:
            raise StudioMetadataError('STUDIO_CATEGORY_REVEAL_FAILED') from None
        control = self._read()
        value = self._target(category_id, control)
        if control.value != value:
            try:
                self._adapter.select_category(value)
            except Exception:
                raise StudioMetadataError('STUDIO_CATEGORY_SELECT_FAILED') from None
        selected = self._read()
        if selected.locale != control.locale or self._target(category_id, selected) != value or selected.value != value:
            raise StudioMetadataError('STUDIO_CATEGORY_SET_MISMATCH')
        return PreparedCategory(category_id, control.locale, value, self._provenance)

    def _validate_prepared(self, prepared):
        if (not isinstance(prepared, PreparedCategory)
                or any(not isinstance(value, str) for value in (
                    prepared.category_id, prepared.locale, prepared.value, prepared.provenance))
                or prepared.provenance != self._provenance
                or self._maps.get(prepared.locale, {}).get(prepared.category_id) != prepared.value):
            raise StudioMetadataError('STUDIO_CATEGORY_PREPARATION_INVALID')

    def recheck(self, prepared: PreparedCategory, *, verify_binding: Callable[[], None],
                verify_approval: Callable[[], None]) -> None:
        """Call immediately before the caller's save/publish side effect."""
        self._validate_prepared(prepared)
        if not callable(verify_binding) or not callable(verify_approval):
            raise StudioMetadataError('STUDIO_CATEGORY_GUARD_REQUIRED')
        for callback, code in ((verify_binding, 'STUDIO_CATEGORY_BINDING_CHANGED'),
                               (verify_approval, 'STUDIO_CATEGORY_APPROVAL_STALE')):
            try:
                if callback() is False:
                    raise ValueError()
            except Exception:
                raise StudioMetadataError(code) from None
        control = self._read()
        if control.locale != prepared.locale or self._target(prepared.category_id, control) != prepared.value or control.value != prepared.value:
            raise StudioMetadataError('STUDIO_CATEGORY_CHANGED_BEFORE_PUBLISH')

    def verify_saved(self, prepared: PreparedCategory, video_id: str, *, evidence_sink: Callable) -> SavedCategory:
        """Persist an already-observed ID before exact-ID readback; never publish.

        A readback or sink failure is uncertain after a send. The caller must
        retain its reservation and hand it to NeedsReview without retry/delete.
        """
        try:
            self._validate_prepared(prepared)
        except StudioMetadataError as exc:
            raise StudioMetadataError(exc.code, after_send=True) from None
        if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise StudioMetadataError('STUDIO_CATEGORY_VIDEO_ID_INVALID', after_send=True)
        if not callable(evidence_sink):
            raise StudioMetadataError('STUDIO_CATEGORY_EVIDENCE_REQUIRED', after_send=True)
        base = {'backend': 'playwright', 'video_id': video_id,
                'expected': {'category_id': prepared.category_id}}
        def record(state, code, observed=None):
            try:
                evidence_sink({**base, 'verification_state': state, 'code': code, 'observed': observed or {}})
            except Exception:
                raise StudioMetadataError('STUDIO_CATEGORY_EVIDENCE_FAILED', after_send=True) from None
        record('received', 'STUDIO_CATEGORY_ID_RECEIVED')
        try:
            saved = self._adapter.read_saved_category(video_id)
        except Exception:
            record('unknown', 'STUDIO_CATEGORY_READBACK_FAILED')
            raise StudioMetadataError('STUDIO_CATEGORY_READBACK_FAILED', after_send=True) from None
        if (not isinstance(saved, SavedCategory) or any(not isinstance(value, str)
                for value in (saved.video_id, saved.locale, saved.value))):
            record('unknown', 'STUDIO_CATEGORY_READBACK_INVALID')
            raise StudioMetadataError('STUDIO_CATEGORY_READBACK_INVALID', after_send=True)
        if saved.video_id != video_id:
            record('unknown', 'STUDIO_CATEGORY_READBACK_ID_MISMATCH')
            raise StudioMetadataError('STUDIO_CATEGORY_READBACK_ID_MISMATCH', after_send=True)
        if saved.locale != prepared.locale:
            record('unknown', 'STUDIO_CATEGORY_READBACK_LOCALE_CHANGED')
            raise StudioMetadataError('STUDIO_CATEGORY_READBACK_LOCALE_CHANGED', after_send=True)
        reverse = {value: category for category, value in self._maps[saved.locale].items()}
        category = reverse.get(saved.value)
        if category is None:
            record('unknown', 'STUDIO_CATEGORY_READBACK_UNMAPPED')
            raise StudioMetadataError('STUDIO_CATEGORY_READBACK_UNMAPPED', after_send=True)
        observed = {'category_id': category}
        if category != prepared.category_id:
            record('mismatch', 'STUDIO_CATEGORY_READBACK_MISMATCH', observed)
            raise StudioMetadataError('STUDIO_CATEGORY_READBACK_MISMATCH', after_send=True)
        record('verified', 'STUDIO_CATEGORY_VERIFIED', observed)
        return saved
