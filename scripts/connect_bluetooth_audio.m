// Connect a paired Bluetooth device by name/address.
// Build: clang -fobjc-arc scripts/connect_bluetooth_audio.m -framework Foundation -framework IOBluetooth -o runtime/connect_bluetooth_audio
#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>

static IOBluetoothDevice *findDevice(NSString *query) {
    NSString *q = [query lowercaseString];
    for (IOBluetoothDevice *d in [IOBluetoothDevice pairedDevices]) {
        NSString *name = [[d nameOrAddress] lowercaseString];
        NSString *addr = [[d addressString] lowercaseString];
        if ([name isEqualToString:q] || [name containsString:q] || [addr isEqualToString:q]) return d;
    }
    return nil;
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSString *query = argc > 1 ? [NSString stringWithUTF8String:argv[1]] : @"";
        if (argc < 2 || [query isEqualToString:@"--list"]) {
            for (IOBluetoothDevice *d in [IOBluetoothDevice pairedDevices]) {
                printf("%s\t%s\t%s\n", [[d nameOrAddress] UTF8String], [[d addressString] UTF8String], [d isConnected] ? "connected" : "disconnected");
            }
            return 0;
        }

        IOBluetoothDevice *device = findDevice(query);
        if (!device) {
            fprintf(stderr, "bluetooth device not found: %s\n", [query UTF8String]);
            return 2;
        }
        if ([device isConnected]) {
            printf("bluetooth already connected: %s\n", [[device nameOrAddress] UTF8String]);
            return 0;
        }

        IOReturn ret = [device openConnection];
        if (ret != kIOReturnSuccess) {
            fprintf(stderr, "bluetooth connect failed: %s status=0x%x\n", [[device nameOrAddress] UTF8String], ret);
            return 1;
        }

        for (int i = 0; i < 30; i++) {
            if ([device isConnected]) break;
            [NSThread sleepForTimeInterval:0.2];
        }
        printf("bluetooth connected: %s\n", [[device nameOrAddress] UTF8String]);
        return [device isConnected] ? 0 : 1;
    }
}
