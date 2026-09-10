# Sources and licensing

This project uses code from Testbed for Learned Indexes (TLI):

- Repository: https://github.com/curtis-sun/TLI
- Commit: `13224d31d2dcb8ad4481529154c4c286cf4c32fc`
- TLI's top-level license is GPL-3.0. Preserve the upstream license and embedded notices for individual competitors and utilities.
- The local entrypoint includes TLI's benchmark, ART implementation, allocator utility and command-line parser. The generator is built directly from upstream source.

GRE is downloaded as reference material only:

- Repository: https://github.com/gre4index/GRE
- Commit: `e807edcef51df6732f07f94d4c797fb3897519ba`
- Paper: Chaichon Wongkham et al., “Are Updatable Learned Indexes Ready?”, PVLDB 15(11), 3004–3017, 2022.
- Do not assume the license of this repository grants redistribution rights to GRE or any of its individual dependencies; inspect their notices before incorporating code.

Boost headers are obtained from the Ubuntu development package or an existing system installation. Their original license notices remain in the local dependency directory. No dependency source, package archive, attachment or dataset is redistributed in this repository.

Original setup scripts and adapter code in this repository are licensed under GPL-3.0; see LICENSE. The GPL text is copied from TLI's LICENSE.
