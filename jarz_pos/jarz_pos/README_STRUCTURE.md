This directory contains the canonical module slug package for the Jarz POS app.

All DocTypes must live under:

```
jarz_pos/jarz_pos/jarz_pos/doctype/<doctype>/<doctype>.json
```

If you previously had DocTypes in the legacy path `jarz_pos/jarz_pos/doctype/`,
they have now been relocated here so that `bench migrate` and `reload-doc`
work reliably in all environments.

Remove this note after confirming production has been redeployed with the
new structure.
