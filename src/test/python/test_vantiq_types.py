"""Tests for the Vantiq type support: Currency, the Annotated aliases and the conversions."""
import datetime as dt
import json
import typing
from decimal import Decimal

import pytest

from vantiqsdk import (Currency, GeoJSON, ResourceReference, VantiqType, from_vantiq, to_vantiq,
                       vantiq_json_default)


# ---- VantiqType and the aliases

def test_vantiq_type_equality_and_repr():
    assert VantiqType('Currency') == VantiqType('Currency')
    assert VantiqType('Currency') != VantiqType('Decimal')
    assert len({VantiqType('GeoJSON'), VantiqType('GeoJSON')}) == 1
    assert repr(VantiqType('GeoJSON')) == "VantiqType('GeoJSON')"


def test_aliases_carry_vantiq_type_metadata():
    def proc(shape: GeoJSON, ref: ResourceReference) -> GeoJSON:
        return shape

    hints = typing.get_type_hints(proc, include_extras=True)
    assert hints['shape'].__metadata__ == (VantiqType('GeoJSON'),)
    assert hints['ref'].__metadata__ == (VantiqType('ResourceReference'),)
    assert hints['shape'].__origin__ is dict
    assert hints['ref'].__origin__ is str


# ---- Currency

def test_currency_from_internal_map():
    c = Currency.from_vantiq({'currency': 'USD', 'value': 1234, 'scale': 2})
    assert c == Currency(Decimal('12.34'), 'USD')
    assert c.amount == Decimal('12.34') and c.currency == 'USD'


def test_currency_from_external_string():
    assert Currency.from_vantiq('EUR:0.5') == Currency('0.5', 'EUR')
    assert Currency.from_vantiq(Currency(1, 'GBP')) == Currency(1, 'GBP')


@pytest.mark.parametrize('bad', ['12.34', 'USD:', ':12', {'value': 1}, 12.34, None])
def test_currency_rejects_other_forms(bad):
    with pytest.raises(ValueError):
        Currency.from_vantiq(bad)


def test_currency_needs_a_code():
    with pytest.raises(ValueError):
        Currency(1, '')


def test_currency_external_form_is_plain():
    assert str(Currency(Decimal('1E+2'), 'USD')) == 'USD:100'
    assert Currency(Decimal('12.340'), 'USD').to_vantiq() == 'USD:12.340'
    assert Currency(12.5, 'USD').to_vantiq() == 'USD:12.5'


def test_currency_arithmetic_keeps_code():
    c = Currency('10.00', 'USD')
    assert c + Currency('0.50', 'USD') == Currency('10.50', 'USD')
    assert c - 1 == Currency('9.00', 'USD')
    assert 1 - c == Currency('-9.00', 'USD')
    assert c * 2 == Currency('20.00', 'USD')
    assert Decimal('0.5') * c == Currency('5.000', 'USD')
    assert c / 4 == Currency('2.5', 'USD')
    assert -c == Currency('-10.00', 'USD') and abs(-c) == c
    assert round(Currency('1.005', 'USD'), 2) == Currency('1.00', 'USD')
    assert bool(Currency(0, 'USD')) is False and bool(c) is True


def test_currency_comparison():
    assert Currency(1, 'USD') < Currency(2, 'USD') <= Currency(2, 'USD')
    assert Currency(3, 'USD') > 2 and Currency(2, 'USD') >= Decimal(2)
    assert Currency(1, 'USD') != Currency(1, 'EUR')
    assert hash(Currency('1.0', 'USD')) == hash(Currency(1, 'USD'))


def test_currency_mixed_codes_raise():
    with pytest.raises(ValueError):
        Currency(1, 'USD') + Currency(1, 'EUR')
    with pytest.raises(ValueError):
        Currency(1, 'USD') < Currency(1, 'EUR')


def test_currency_unsupported_operands():
    with pytest.raises(TypeError):
        Currency(1, 'USD') + 'x'
    with pytest.raises(TypeError):
        Currency(1, 'USD') * Currency(1, 'USD')


# ---- from_vantiq

def test_from_vantiq_by_declared_type():
    assert from_vantiq('USD:1.5', 'Currency') == Currency('1.5', 'USD')
    assert from_vantiq({'currency': 'USD', 'value': 150, 'scale': 2}, 'Currency') == Currency('1.50', 'USD')
    assert from_vantiq('12.34', 'Decimal') == Decimal('12.34')
    assert from_vantiq(12.5, 'Decimal') == Decimal('12.5')
    assert from_vantiq({'value': 1234, 'scale': 2}, 'Decimal') == Decimal('12.34')
    assert from_vantiq(Decimal('1'), 'Decimal') == Decimal('1')
    assert from_vantiq({'resource': 'types', 'resourceId': 'Order'}, 'ResourceReference') == '/types/Order'
    assert from_vantiq('/types/Order', 'ResourceReference') == '/types/Order'
    assert from_vantiq('x', 'String') == 'x'


def test_from_vantiq_datetime():
    assert from_vantiq('2024-01-02T03:04:05Z', 'DateTime') == dt.datetime(2024, 1, 2, 3, 4, 5,
                                                                          tzinfo=dt.timezone.utc)
    parsed = from_vantiq('2024-01-02T03:04:05.123+01:00', 'DateTime')
    assert parsed.microsecond == 123000 and parsed.utcoffset() == dt.timedelta(hours=1)


def test_from_vantiq_infers_internal_maps_only():
    assert from_vantiq({'currency': 'USD', 'value': 1, 'scale': 0}) == Currency(1, 'USD')
    assert from_vantiq({'value': 5, 'scale': 1}) == Decimal('0.5')
    assert from_vantiq({'resource': 'types', 'resourceId': 'A'}) == '/types/A'
    assert from_vantiq('USD:1') == 'USD:1'
    assert from_vantiq({'value': 1, 'scale': 0, 'extra': True}) == {'value': 1, 'scale': 0, 'extra': True}


def test_from_vantiq_lists():
    assert from_vantiq(['USD:1', 'USD:2'], 'Currency') == [Currency(1, 'USD'), Currency(2, 'USD')]


# ---- to_vantiq and the json default

def test_to_vantiq_recurses():
    value = {'price': Currency('1.5', 'USD'), 'items': [Decimal('2.0'), (Decimal('1E+1'), 'x')],
             'when': dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc), 'n': 3}
    assert to_vantiq(value) == {'price': 'USD:1.5', 'items': ['2.0', ['10', 'x']],
                                'when': '2024-01-02T03:04:05Z', 'n': 3}


def test_json_default_covers_vantiq_types():
    naive = dt.datetime(2024, 1, 2, 3, 4, 5, 500000)
    payload = {'c': Currency('1.00', 'USD'), 'd': Decimal('0.1'), 'dt': naive,
               'date': dt.date(2024, 1, 2), 'time': dt.time(3, 4), 'b': b'hi'}
    assert json.loads(json.dumps(payload, default=vantiq_json_default)) == {
        'c': 'USD:1.00', 'd': '0.1', 'dt': '2024-01-02T03:04:05.500000Z',
        'date': '2024-01-02', 'time': '03:04:00', 'b': 'aGk='}


def test_json_default_keeps_non_utc_offset():
    aware = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert vantiq_json_default(aware) == '2024-01-02T03:04:05+02:00'


def test_json_default_rejects_unknown():
    with pytest.raises(TypeError):
        vantiq_json_default(object())
