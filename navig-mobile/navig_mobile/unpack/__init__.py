"""Game / app asset extraction for Android packages — ``navig mobile unpack``.

Turns an APK (+ splits, OBB data files, downloaded ``Android/data`` content) into readable data
and media: Unity (text assets, decoded MonoBehaviours incl. IL2CPP type trees, textures, audio,
video, fonts, meshes), XNA/MonoGame XNB (Stardew-style tables, textures, wave banks), Kairosoft
``.bin`` archives, HTML5/Cordova games, Wwise/Bink/DDS media, Ogg carving from unknown archives.

Heavy dependencies are optional (``pip install navig-mobile[unpack]``) and imported lazily;
external tools (7-Zip, ffmpeg, vgmstream, Il2CppDumper) are resolved by :mod:`.tools`.
"""
