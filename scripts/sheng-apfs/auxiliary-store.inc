
// macOS searches the physical store's content hint for iSC-only role mounts.
// This experimental combined container owns those roles as well as System/Data.
extern "C" int (*native_apfs_auxiliary_store_hook)(dev_t);
static int native_apfs_publish_auxiliary_store(dev_t device)
{
    if (!verifiedVolumeQuery(device, nullptr, nullptr)) return 0;
    OSDictionary *match = IOService::serviceMatching("IOMedia");
    OSDictionary *properties = OSDictionary::withCapacity(2);
    OSNumber *mjr = OSNumber::withNumber(static_cast<unsigned>(major(static_cast<uint32_t>(device))), 32);
    OSNumber *mnr = OSNumber::withNumber(static_cast<unsigned>(minor(static_cast<uint32_t>(device))), 32);
    const bool ready = match && properties && mjr && mnr &&
        properties->setObject(kIOBSDMajorKey, mjr) && properties->setObject(kIOBSDMinorKey, mnr) &&
        match->setObject("IOPropertyMatch", properties);
    OSSafeReleaseNULL(properties); OSSafeReleaseNULL(mjr); OSSafeReleaseNULL(mnr);
    OSIterator *iterator = ready ? IOService::getMatchingServices(match) : nullptr;
    OSSafeReleaseNULL(match);
    if (!iterator) return 0;
    int result = 0;
    while (OSObject *object = iterator->getNextObject()) {
        IOService *media = OSDynamicCast(IOService, object);
        bool insideContainer = false;
        unsigned depth = 0;
        for (IOService *provider = media; provider && depth < 64; provider = provider->getProvider(), ++depth) {
            if (provider->metaCast("AppleAPFSContainer")) insideContainer = true;
            if (!insideContainer || !provider->metaCast("IOMedia") || provider->metaCast("AppleAPFSMedia")) continue;
            OSString *hint = OSDynamicCast(OSString, provider->getProperty("Content Hint"));
            const char *isc = "69646961-6700-11AA-AA11-00306543ECAC";
            if (hint && !hint->isEqualTo("") && !hint->isEqualTo("7C3457EF-0000-11AA-AA11-00306543ECAC") &&
                !hint->isEqualTo(isc)) break;
            result = provider->setProperty("Content Hint", isc) &&
                     provider->setProperty("UniXNUCombinedSystemISC", true);
            break;
        }
    }
    iterator->release();
    printf("Native APFS - combined iSC role store published: %d\n", result);
    return result;
}
extern "C" void native_apfs_auxiliary_store_init(void);
extern "C" void native_apfs_auxiliary_store_init(void)
{
    native_apfs_auxiliary_store_hook = native_apfs_publish_auxiliary_store;
}
