/** Bounded JVM monitor-contention helper used only by the VM benchmark. */
public final class JavaLockFault {
    private static final Object LOCK = new Object();

    public static void main(String[] args) throws Exception {
        int seconds = args.length > 0 ? Math.min(Integer.parseInt(args[0]), 600) : 180;
        int workers = args.length > 1 ? Math.min(Integer.parseInt(args[1]), 64) : 16;
        synchronized (LOCK) {
            for (int i = 0; i < workers; i++) {
                Thread thread = new Thread(() -> {
                    synchronized (LOCK) {
                        // Reached only when the bounded benchmark exits.
                    }
                }, "blocked-worker-" + i);
                thread.setDaemon(true);
                thread.start();
            }
            Thread.sleep(seconds * 1000L);
        }
    }
}
