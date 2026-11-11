    unsigned virtio = 0;
    if (PE_parse_boot_argn("native-apfs-virtio", &virtio, sizeof(virtio)) && virtio == 1) {
        unsigned started = 0;
        OSDictionary *match = IOService::serviceMatching("IOPlatformDevice");
        OSIterator *devices = match ? IOService::getMatchingServices(match) : nullptr;
        OSSafeReleaseNULL(match);
        if (!devices) panic("SourceStorage: no QEMU platform devices");
        while (OSObject *object = devices->getNextObject()) {
            IOService *provider = OSDynamicCast(IOService, object);
            if (!provider) continue;
            IOService *disk = OSDynamicCast(IOService, OSMetaClass::allocClassWithName("NativeVirtioBlock"));
            if (!disk) panic("SourceStorage: cannot allocate QEMU block transport");
            SInt32 score = 0;
            if (!disk->init() || !disk->probe(provider, &score)) { disk->release(); continue; }
            if (!disk->attach(provider)) { disk->release(); continue; }
            if (!disk->start(provider)) { disk->detach(provider); disk->release(); continue; }
            ++started;
        }
        devices->release();
        if (started != 1) panic("SourceStorage: expected one bound QEMU APFS disk, got %u", started);
        printf("SourceStorage: QEMU APFS transport started\n");
        return;
    }
