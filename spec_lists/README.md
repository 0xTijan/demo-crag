# Spec lists

One YAML file per component type (`<type>.yaml`, the key from `index_cache/type_list.yaml`): every
specification that most datasheets of the type have in common, with how a replacement part is compared
(plan v1, `parts_alternatives_plan.md`, "Spec lists").

Drafted with `python -m dataPipeline.index.spec_lists draft <type>`, reviewed by hand (`status: reviewed`),
validated with `python -m dataPipeline.index.spec_lists check`. See `dataPipeline/index/README.md`.

A reviewed list is never overwritten by a new draft; that goes to `<type>.draft.yaml` for comparison.
Kept in git: raise `version` on every change after review.
