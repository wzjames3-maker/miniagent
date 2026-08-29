# TinyBilling

A small subscription billing helper library.

## Layout

- `billing.py` - the implementation
- `test_billing.py` - the pytest suite

## Rules

- Money is handled in whole cents (integers), never floats.
- Coupons: `SAVE<n>` = n percent off, `FLAT<n>` = n dollars off.
- Annual plans (`months >= 12`) are billed as 10 months.

## Run the tests

```bash
python -m pytest test_billing.py -q
```
