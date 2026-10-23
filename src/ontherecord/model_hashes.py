"""The files of each Hugging Face model in the catalog that On the Record downloads (its `patterns`), at its pinned
revision, with their hashes: sha256 for large (LFS) files, git's blob id for small ones. Written from Hugging Face's
API on Oct 23, 2026. Setup checks every downloaded file against these, so what runs is what was pinned, whatever
the network in between served."""

FILES = {
    "whisper-large-v3": {  # Systran/faster-whisper-large-v3 @ edaa852ec7e1
        "config.json": "git:75336feae814999bae6ccccdecf177639ffc6f9d",
        "model.bin": "sha256:69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
        "preprocessor_config.json": "git:931c77a740890c46365c7ae0c9d350ba3cca908f",
        "tokenizer.json": "git:3a5e2ba63acdcac9a19ba56cf9bd27f185bfff61",
        "vocabulary.json": "git:0adcd01e7c237205d593b707e66dd5d7bc785d2d",
    },
    "whisper-large-v3-turbo": {  # mobiuslabsgmbh/faster-whisper-large-v3-turbo @ 0a363e9161cb
        "config.json": "git:0351d1d6870005e865747b781b5d7c23ea0459cd",
        "model.bin": "sha256:e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
        "preprocessor_config.json": "git:931c77a740890c46365c7ae0c9d350ba3cca908f",
        "tokenizer.json": "git:17456db595adc78a973f97d69d8cb50bc87c0b1c",
        "vocabulary.json": "git:0adcd01e7c237205d593b707e66dd5d7bc785d2d",
    },
    "pyannote-community-1": {  # pyannote/speaker-diarization-community-1 @ 3533c8cf8e36
        "config.yaml": "git:4022db43960736338378fdb6b5a85cfdae198910",
        "embedding/pytorch_model.bin": "sha256:6f10ff60898a1d185fa22e1d11e0bfa8a92efec811f11bca48cb8cafebefd929",
        "plda/plda.npz": "sha256:9b77bcd840692710dd3496f62ecfeed8d8e5f002fd991b785079b244eab7d255",
        "plda/xvec_transform.npz": "sha256:325f1ce8e48f7e55e9c8aa47e05d2766b7c48c4b25b8de8dd751e7a4cc5fbe8f",
        "segmentation/pytorch_model.bin": "sha256:7ad24338d844fb95985486eb1a464e32d229f6d7a03c9abe60f978bacf3f816e",
    },
    "whistress": {  # slprl/WhiStress @ 3641786c97df
        "additional_decoder_block.pt": "sha256:b7d440821c831364c5046e859843926120550a38143f89e1bace82a2ed03cc77",
        "classifier.pt": "sha256:599257b647cbca9fc21aac4ede87651cd43d03c3338e705bd59d919ee19ebc6f",
        "metadata.json": "git:52d4563cac4d04fb40254eb72afb00990f8c7ae0",
    },
    "whisper-small.en": {  # openai/whisper-small.en @ e8727524f962
        "added_tokens.json": "git:0b661e7eeafa60f4366f802cc18d99a08574861c",
        "config.json": "git:441d82a4be08ee59609c374a4d5082641677466e",
        "generation_config.json": "git:6b3e70541b0f1565a9d01901d0112abc19c88265",
        "merges.txt": "git:226b0752cac7789c48f0cb3ec53eda48b7be36cc",
        "model.safetensors": "sha256:6014ac49b506df900f66f4aca6b0801eed7245594ace97bcaf73e0ae5b863066",
        "normalizer.json": "git:dd6ae819ad738ac1a546e9f9282ef325c33b9ea0",
        "preprocessor_config.json": "git:c2048dfa9fd94a052e62e908d2c4dfb18534b4d2",
        "special_tokens_map.json": "git:7e6b5b4f006274b7085b597005a9ad73c639eb80",
        "tokenizer.json": "git:b5bd266de2f9b9a68efe13fb48ac6830ca71c0bb",
        "tokenizer_config.json": "git:6b53d4f2b480c9267ffc419c7628159d25f3072a",
        "vocab.json": "git:84ef7fb594b5c0979e48bdeddb60a0adef33df0b",
    },
}
