# Retained native screenshot results

These fixtures come from the failed natural-browser-r1 trial on 2026-09-23.
`results.json` contains the exact root and delegated computer tool result
envelopes, their original screenshot paths, and the preceding tool-call blocks.
Model reasoning and unrelated conversation content are omitted.

The two PNGs are retained original bytes, each 69,370 bytes with SHA256
`5dc1e850ca1d8f214791d11a06a1d260db762064cd70fd5d0ccb7f835884ac5d`.
Both captures happened without any click; fixture target and decoy counters
remained zero. Byte identity is verified by the regression. A black rendering
of one file in an image viewer was inconsistent with this byte identity and is
not a property attributed to the retained screenshot.

Tests relocate only the screenshot path to this fixture directory. Repeated
turn cases duplicate the retained shape with distinct test call IDs; they are
synthetic offline test histories, not additional claimed runtime activity.
Nothing in the live trial harness or original runtime evidence was modified.

The failed runtime used computer-use `096127f05ddbfedc59d4ee4503c9416465dbfb6c`
and provider-openai `a12ff8b88390ab8f9c885a0cd282b1868ee30709`.
