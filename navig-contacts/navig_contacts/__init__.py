"""navig-contacts — an address book for a navig space.

The engine (vCard parsing, identity normalisation, record linkage, the SQLite
store) is importable on its own:

    from navig_contacts.engine import get_db, init_db, use_base_dir
    from navig_contacts.engine.vcf_import import analyse

The CLI lives in :mod:`navig_contacts.commands.contacts`.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
