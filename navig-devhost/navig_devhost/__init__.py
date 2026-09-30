"""navig-devhost — local dev domains with trusted HTTPS, from `navig devhost`.

Give any local dev server a real `.test` domain over trusted HTTPS in one command:
`navig devhost add cybesis.test --port 7645` then `navig devhost up`. A first-party
navig plugin (free, toggleable). Hosts entry + mkcert cert + a raw TLS relay — no
per-project proxy scripts.
"""

__version__ = "0.1.0"
