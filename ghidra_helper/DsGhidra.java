// Headless Ghidra driver for Decompiler Studio.
//
//   ghidra.Ghidra DsGhidra <input> <output.c> <analysisSeconds> <decompileSeconds> <analyze:1|0>
//
// Imports <input>, runs Ghidra's auto-analysis (unless analyze=0), then decompiles every
// function in parallel and writes one C file: the recovered type definitions, prototypes, and
// the function bodies in address order.
//
// Progress lines start with "[ds]" so the app can show them. Both time limits keep whatever
// was finished: a timed-out analysis is still decompiled, and a timed-out decompile still
// writes every function that completed.
//
// The jar lives in a tiny Ghidra module (Ghidra/Features/DecompilerStudio/lib) so that Ghidra's
// own launcher puts it on the class path together with Ghidra's jars.
import java.io.File;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import ghidra.GhidraApplicationLayout;
import ghidra.GhidraLaunchable;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.decompiler.DecompiledFunction;
import ghidra.app.decompiler.parallel.DecompilerCallback;
import ghidra.app.decompiler.parallel.ParallelDecompiler;
import ghidra.app.plugin.core.analysis.AutoAnalysisManager;
import ghidra.app.util.exporter.CppExporter;
import ghidra.app.util.importer.ProgramLoader;
import ghidra.app.util.opinion.LoadResults;
import ghidra.framework.Application;
import ghidra.framework.options.Options;
import ghidra.framework.options.OptionType;
import ghidra.framework.HeadlessGhidraApplicationConfiguration;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.data.TerminatedStringDataType;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Program;
import ghidra.program.util.GhidraProgramUtilities;
import ghidra.util.task.TaskMonitor;
import ghidra.util.task.TimeoutTaskMonitor;

public class DsGhidra implements GhidraLaunchable {
    static final String HELPER_VERSION = "6";


    static final int PER_FUNCTION_SECONDS = 30;

    // Compiler / C-runtime start-up code that is in every program and is not the author's code.
    static final Set<String> BOILERPLATE = Set.of(
        "_init", "_fini", "_start", "entry", "frame_dummy", "register_tm_clones", "deregister_tm_clones",
        "__do_global_dtors_aux", "__libc_csu_init", "__libc_csu_fini", "_dl_relocate_static_pie",
        "__do_global_ctors_aux", "_do_init", "_do_fini");
    static final Set<String> BOILERPLATE_BLOCKS = Set.of(
        ".init", ".fini", ".plt", ".plt.got", ".plt.sec", ".plt.bnd", ".iplt");

    static boolean isBoilerplate(Program prog, Function f) {
        if (System.getenv("DS_KEEP_BOILERPLATE") != null) {
            return false;
        }
        if (BOILERPLATE.contains(f.getName())) {
            return true;
        }
        MemoryBlock b = prog.getMemory().getBlock(f.getEntryPoint());
        return b != null && BOILERPLATE_BLOCKS.contains(b.getName());
    }

    static void say(String s) {
        System.out.println("[ds] " + s);
        System.out.flush();
    }

    /** One decompiled function (or the reason it could not be decompiled). */
    static final class Res {
        final Address addr;
        final String name;
        final String sig;
        final String c;
        final String error;

        Res(Address addr, String name, String sig, String c, String error) {
            this.addr = addr;
            this.name = name;
            this.sig = sig;
            this.c = c;
            this.error = error;
        }
    }

    @Override
    public void launch(GhidraApplicationLayout layout, String[] a) {
        int rc = 1;
        try {
            rc = run(layout, a);
        }
        catch (Throwable t) {
            say("Ghidra helper crashed: " + t);
            t.printStackTrace(System.out);
        }
        System.out.flush();
        System.exit(rc);
    }

    /** Prints a status line every few seconds so the app can show signs of life. */
    static Thread ticker(String phase, Supplier<String> status) {
        Thread t = new Thread(() -> {
            String last = "";
            while (!Thread.currentThread().isInterrupted()) {
                try {
                    Thread.sleep(4000);
                }
                catch (InterruptedException e) {
                    return;
                }
                String line = phase + ": " + status.get();
                if (!line.equals(last)) {
                    say(line);
                    last = line;
                }
            }
        }, "ds-ticker");
        t.setDaemon(true);
        t.start();
        return t;
    }

    static int run(GhidraApplicationLayout layout, String[] a) throws Exception {
        if (a.length < 5) {
            say("usage: DsGhidra <input> <output.c> <analysisSeconds> <decompileSeconds> <analyze 1|0>");
            return 2;
        }
        File input = new File(a[0]);
        File out = new File(a[1]);
        long analysisSecs = Long.parseLong(a[2]);
        long decompSecs = Long.parseLong(a[3]);
        boolean analyze = a[4].equals("1");

        HeadlessGhidraApplicationConfiguration cfg = new HeadlessGhidraApplicationConfiguration();
        cfg.setInitializeLogging(false);
        Application.initializeApplication(layout, cfg);
        say("Ghidra " + Application.getApplicationVersion() + " started (helper v" + HELPER_VERSION + ")");

        Object consumer = new Object();
        Program prog = null;
        try {
            say("Importing " + input.getName() + " ...");
            try (LoadResults<Program> lr = ProgramLoader.builder().source(input).monitor(TaskMonitor.DUMMY).load()) {
                prog = lr.getPrimaryDomainObject(consumer);
            }
            catch (Exception e) {
                say("Ghidra could not import this file: " + e.getMessage());
                return 3;
            }
            say("Imported as " + prog.getLanguageID() + " / " + prog.getCompilerSpec().getCompilerSpecID()
                + ", " + prog.getFunctionManager().getFunctionCount() + " function(s) known before analysis");

            try {
                discoverFunctions(prog, Math.max(60, analysisSecs / 2));
            }
            catch (Throwable t) {
                say("Function search skipped: " + t);
            }
            if (analyze) {
                analyse(prog, analysisSecs);
            }
            else {
                say("Skipping auto-analysis (quick mode).");
            }
            return decompileAll(prog, out, decompSecs);
        }
        finally {
            if (prog != null) {
                prog.release(consumer);
            }
        }
    }

    /**
     * Programs without symbols for their code (Flutter's libapp.so, stripped/packed libraries) give
     * Ghidra no place to start, so it finds no functions at all. Find function starts in the machine
     * code itself: AArch64 frame set-ups (stp x29, x30, [sp or x15, #-N]!, paciasp, bti c) and the
     * targets of every bl call; x86 push rbp / mov rbp, rsp and endbr64.
     */
    static int discoverFunctions(Program prog, long secs) throws Exception {
        Memory mem = prog.getMemory();
        String proc = prog.getLanguage().getProcessor().toString();
        boolean a64 = proc.equalsIgnoreCase("AARCH64");
        boolean x86 = proc.equalsIgnoreCase("x86");
        if (!a64 && !x86) {
            return 0;
        }
        long codeBytes = 0;
        List<MemoryBlock> code = new ArrayList<>();
        for (MemoryBlock b : mem.getBlocks()) {
            if (b.isExecute() && b.isInitialized() && b.getSize() < Integer.MAX_VALUE
                    && !BOILERPLATE_BLOCKS.contains(b.getName())) {
                code.add(b);
                codeBytes += b.getSize();
            }
        }
        int have = 0;
        for (Function f : prog.getFunctionManager().getFunctions(true)) {
            MemoryBlock b = mem.getBlock(f.getEntryPoint());
            if (!f.isExternal() && b != null && code.contains(b)) {
                have++;
            }
        }
        // a normal program has a function every few hundred bytes; one per 16 KB means symbols are missing
        if (codeBytes < 32 * 1024 || (long) have * 16384 >= codeBytes) {
            return 0;
        }
        say("Only " + have + " function(s) known for " + (codeBytes / 1024) + " KB of code (no symbols): "
            + "searching the machine code for function starts...");
        AddressSet starts = new AddressSet();
        for (MemoryBlock b : code) {
            byte[] buf = new byte[(int) b.getSize()];
            int n = b.getBytes(b.getStart(), buf);
            Address base = b.getStart();
            if (a64) {
                long off0 = (4 - (base.getOffset() & 3)) & 3;
                for (int i = (int) off0; i + 4 <= n; i += 4) {
                    int w = (buf[i] & 0xff) | (buf[i + 1] & 0xff) << 8 | (buf[i + 2] & 0xff) << 16 | (buf[i + 3] & 0xff) << 24;
                    if ((w & 0xFFC07C1F) == 0xA980781D) {              // stp x29, x30, [xN, #-imm]!
                        int at = i;
                        if (i >= 4) {
                            int p = (buf[i - 4] & 0xff) | (buf[i - 3] & 0xff) << 8 | (buf[i - 2] & 0xff) << 16 | (buf[i - 1] & 0xff) << 24;
                            if (p == 0xD503233F || p == 0xD503245F || p == 0xD503249F || p == 0xD50324DF) {
                                at = i - 4;                             // paciasp / bti c|j|jc in front
                            }
                        }
                        starts.add(base.add(at));
                    }
                    else if ((w & 0xFC000000) == 0x94000000) {          // bl <target>
                        long imm = w & 0x03FFFFFF;
                        if ((imm & 0x02000000) != 0) {
                            imm -= 0x04000000;
                        }
                        long t = i + imm * 4;
                        if (t >= 0 && t + 4 <= n) {
                            int tw = (buf[(int) t] & 0xff) | (buf[(int) t + 1] & 0xff) << 8;
                            if (tw != 0 || buf[(int) t + 2] != 0 || buf[(int) t + 3] != 0) {
                                starts.add(base.add(t));
                            }
                        }
                    }
                }
            }
            else {
                for (int i = 0; i + 4 <= n; i++) {
                    int b0 = buf[i] & 0xff, b1 = buf[i + 1] & 0xff, b2 = buf[i + 2] & 0xff, b3 = buf[i + 3] & 0xff;
                    if (b0 == 0xF3 && b1 == 0x0F && b2 == 0x1E && (b3 == 0xFA || b3 == 0xFB)) {
                        starts.add(base.add(i));                        // endbr64 / endbr32
                        i += 3;
                    }
                    else if (b0 == 0x55 && ((b1 == 0x48 && b2 == 0x89 && b3 == 0xE5) || (b1 == 0x89 && b2 == 0xE5))) {
                        if (i == 0 || !(buf[i - 1] == (byte) 0xFA && i >= 4 && (buf[i - 4] & 0xff) == 0xF3)) {
                            starts.add(base.add(i));                    // push rbp; mov rbp, rsp
                        }
                    }
                }
            }
        }
        // drop addresses that already belong to a known function
        AddressSet fresh = new AddressSet();
        for (Address a : starts.getAddresses(true)) {
            if (prog.getFunctionManager().getFunctionContaining(a) == null) {
                fresh.add(a);
            }
        }
        long count = fresh.getNumAddresses();
        if (count == 0) {
            say("No function starts found in the code.");
            return 0;
        }
        say("Found " + count + " likely function start(s); building functions from them...");
        TimeoutTaskMonitor mon = TimeoutTaskMonitor.timeoutIn(secs, TimeUnit.SECONDS);
        AtomicInteger made = new AtomicInteger();
        Thread tk = ticker("Building functions", () -> made.get() + "/" + count);
        int tx = prog.startTransaction("Decompiler Studio function search");
        try {
            Listing listing = prog.getListing();
            // a symbol like _kDartSnapshotText makes Ghidra define the whole code section as one data
            // object, which blocks disassembly: clear data (not instructions) from the code blocks
            for (MemoryBlock b : code) {
                Data d = listing.getDefinedDataContaining(b.getStart());
                boolean anyCode = listing.getInstructions(new AddressSet(b.getStart(), b.getEnd()), true).hasNext();
                if (!anyCode) {
                    listing.clearCodeUnits(b.getStart(), b.getEnd(), false);
                }
                else if (d != null) {
                    listing.clearCodeUnits(d.getMinAddress(), d.getMaxAddress(), false);
                }
            }
            DisassembleCommand dis = new DisassembleCommand(fresh, null, true);
            dis.applyTo(prog, mon);
            for (Address a : fresh.getAddresses(true)) {
                if (mon.isCancelled()) {
                    break;
                }
                if (listing.getInstructionAt(a) == null || prog.getFunctionManager().getFunctionAt(a) != null) {
                    continue;
                }
                CreateFunctionCmd cf = new CreateFunctionCmd(a);
                if (cf.applyTo(prog, TaskMonitor.DUMMY)) {
                    made.incrementAndGet();
                }
            }
        }
        finally {
            prog.endTransaction(tx, true);
            tk.interrupt();
        }
        if (mon.didTimeout()) {
            say("Function search hit its " + secs + " s limit; keeping the " + made.get() + " it built.");
        }
        say("Built " + made.get() + " function(s) from the machine code (their names are lost: FUN_<address>).");
        return made.get();
    }

    static void analyse(Program prog, long secs) {
        TimeoutTaskMonitor mon = TimeoutTaskMonitor.timeoutIn(secs, TimeUnit.SECONDS);
        Thread tk = ticker("Analysing", () -> {
            String m = mon.getMessage();
            return m == null || m.isEmpty() ? "working" : m;
        });
        int tx = prog.startTransaction("Decompiler Studio analysis");
        try {
            AutoAnalysisManager mgr = AutoAnalysisManager.getAnalysisManager(prog);
            mgr.initializeOptions();
            // Off by default in Ghidra: asks the decompiler to infer parameter types and names from
            // how callers and callees use them (float vs int, pointers, ...). Found by name so a
            // renamed option in a future Ghidra only costs this nicety, not the run.
            Options ao = prog.getOptions(Program.ANALYSIS_PROPERTIES);
            boolean paramId = false;
            for (String n : ao.getOptionNames()) {
                if (n.contains("Parameter ID") && ao.getType(n) == OptionType.BOOLEAN_TYPE) {
                    ao.setBoolean(n, true);
                    paramId = true;
                }
            }
            say(paramId ? "Parameter type inference: on" : "Parameter type inference: not found in this Ghidra");
            mgr.initializeOptions();
            mgr.reAnalyzeAll(null);
            mgr.startAnalysis(mon);
            GhidraProgramUtilities.markProgramAnalyzed(prog);
        }
        finally {
            prog.endTransaction(tx, true);
            tk.interrupt();
        }
        if (mon.didTimeout()) {
            say("Analysis hit its " + secs + " s limit; continuing with what it found.");
        }
        try {
            int made = defineShortStrings(prog);
            if (made > 0) {
                say("Recognised " + made + " short text string(s) (like \"%d\") so they print as text.");
            }
        }
        catch (Throwable t) {
            say("Short-string pass skipped: " + t);
        }
        say("Analysis done: " + prog.getFunctionManager().getFunctionCount() + " function(s)");
    }

    /** Ghidra only labels text of 5+ characters; scanf("%d") style formats stay as DAT_xxxxxxxx. */
    static int defineShortStrings(Program prog) throws Exception {
        Listing listing = prog.getListing();
        Memory mem = prog.getMemory();
        ReferenceManager rm = prog.getReferenceManager();
        AddressSet code = new AddressSet();
        for (MemoryBlock b : mem.getBlocks()) {
            if (b.isExecute() && b.isInitialized()) {
                code.add(b.getStart(), b.getEnd());
            }
        }
        int made = 0;
        int tx = prog.startTransaction("Decompiler Studio short strings");
        try {
            AddressIterator it = rm.getReferenceSourceIterator(code, true);
            while (it.hasNext()) {
                for (Reference r : rm.getReferencesFrom(it.next())) {
                    if (!r.isMemoryReference() || r.getReferenceType().isFlow()) {
                        continue;
                    }
                    Address to = r.getToAddress();
                    MemoryBlock tb = mem.getBlock(to);
                    if (tb == null || !tb.isInitialized() || tb.isExecute() || tb.isWrite()) {
                        continue;
                    }
                    Data d = listing.getDataAt(to);
                    if (d != null && d.isDefined() && !d.getDataType().getName().startsWith("undefined")) {
                        continue;
                    }
                    int len = 0;
                    boolean ok = false;
                    for (; len < 80; len++) {
                        Address a = to.add(len);
                        if (!tb.contains(a)) {
                            break;
                        }
                        int c = mem.getByte(a) & 0xff;
                        if (c == 0) {
                            ok = len >= 2;
                            break;
                        }
                        if (!((c >= 0x20 && c < 0x7f) || c == 10 || c == 9 || c == 27)) {
                            break;
                        }
                    }
                    if (!ok) {
                        continue;
                    }
                    Address end = to.add(len);                 // includes the terminating 0
                    if (listing.getInstructionContaining(to) != null) {
                        continue;
                    }
                    Data after = listing.getDefinedDataContaining(to);
                    if (after != null && !after.getMinAddress().equals(to)) {
                        continue;
                    }
                    try {
                        listing.clearCodeUnits(to, end, false);
                        listing.createData(to, TerminatedStringDataType.dataType);
                        made++;
                    }
                    catch (Exception e) {
                        // overlaps something real: leave it alone
                    }
                }
            }
        }
        finally {
            prog.endTransaction(tx, true);
        }
        return made;
    }

    static int decompileAll(Program prog, File out, long secs) throws Exception {
        // Type definitions first (CppExporter with nothing to decompile writes just the types).
        String prologue = "";
        File tmp = new File(out.getParentFile(), out.getName() + ".types.tmp");
        try {
            new CppExporter().export(tmp, prog, new AddressSet(), TaskMonitor.DUMMY);
            if (tmp.exists()) {
                prologue = Files.readString(tmp.toPath(), StandardCharsets.UTF_8);
            }
        }
        catch (Exception e) {
            say("Could not write the type definitions: " + e.getMessage());
        }
        finally {
            tmp.delete();
        }

        List<Function> funcs = new ArrayList<>();
        int hidden = 0;
        for (Function f : prog.getFunctionManager().getFunctions(true)) {
            if (f.isExternal() || f.isThunk()) {
                continue;
            }
            if (isBoilerplate(prog, f)) {
                hidden++;
                continue;
            }
            funcs.add(f);
        }
        if (hidden > 0) {
            say("Hiding " + hidden + " compiler start-up function(s) (_start, _init, PLT stubs, ...).");
        }
        say("Decompiling " + funcs.size() + " function(s) (up to " + PER_FUNCTION_SECONDS + " s each, "
            + secs + " s in total)...");

        TreeMap<Address, Res> results = new TreeMap<>();
        AtomicInteger done = new AtomicInteger();
        TimeoutTaskMonitor mon = TimeoutTaskMonitor.timeoutIn(secs, TimeUnit.SECONDS);
        Thread tk = ticker("Decompiling", () -> done.get() + "/" + funcs.size() + " functions");

        DecompilerCallback<Res> cb = new DecompilerCallback<>(prog, d -> {
            DecompileOptions opts = new DecompileOptions();
            opts.grabFromProgram(prog);
            // closer to how people write C
            opts.setInplaceTokens(true);          // x += 1 instead of x = x + 1
            opts.setNullToken(true);              // NULL instead of 0 for pointers
            opts.setConventionPrint(false);       // no calling-convention tags ("processEntry"): not C
            opts.setIndentWidth(4);
            opts.setNoCastPrint(true);            // hide the casts that only exist for the machine
            opts.setIntegerFormat(DecompileOptions.IntegerFormatEnum.Decimal);  // 24, not 0x18
            opts.setFunctionBraceFormat(DecompileOptions.BraceStyle.Same);
            opts.setIfElseBraceFormat(DecompileOptions.BraceStyle.Same);
            opts.setLoopBraceFormat(DecompileOptions.BraceStyle.Same);
            opts.setSwitchBraceFormat(DecompileOptions.BraceStyle.Same);
            d.setOptions(opts);
            d.toggleCCode(true);
            d.toggleSyntaxTree(false);
            d.setSimplificationStyle("decompile");
        }) {
            @Override
            public Res process(DecompileResults r, TaskMonitor m) {
                Function f = r.getFunction();
                if (r.decompileCompleted()) {
                    DecompiledFunction df = r.getDecompiledFunction();
                    return new Res(f.getEntryPoint(), f.getName(), df.getSignature(), df.getC(), null);
                }
                String why = r.isTimedOut() ? "timed out" : r.getErrorMessage();
                return new Res(f.getEntryPoint(), f.getName(), null, null, why == null ? "unknown error" : why);
            }
        };
        cb.setTimeout(PER_FUNCTION_SECONDS);
        try {
            ParallelDecompiler.decompileFunctions(cb, prog, funcs.iterator(), res -> {
                // results arrive in any order; keyed by address so the file comes out sorted
                if (res != null) {
                    synchronized (results) {
                        results.put(res.addr, res);
                    }
                }
                done.incrementAndGet();
            }, mon);
        }
        catch (Exception e) {
            if (!mon.didTimeout()) {
                say("Decompiler stopped early: " + e);
            }
        }
        finally {
            tk.interrupt();
            cb.dispose();
        }
        if (mon.didTimeout()) {
            say("Decompiling hit its " + secs + " s limit; writing the " + results.size() + " function(s) that finished.");
        }

        int good = 0, bad = 0;
        synchronized (results) {
            try (PrintWriter w = new PrintWriter(out, StandardCharsets.UTF_8)) {
                w.println("// Decompiled by Ghidra " + Application.getApplicationVersion() + " (via Decompiler Studio)");
                w.println("// Reconstructed from machine code: names, types and structure are best guesses.");
                w.println("// Architecture: " + prog.getLanguageID() + " / " + prog.getCompilerSpec().getCompilerSpecID());
                w.println();
                if (!prologue.isBlank()) {
                    // type definitions live next to this file so the code starts on the first screen
                    Files.writeString(new File(out.getParentFile(), "types.h").toPath(), prologue, StandardCharsets.UTF_8);
                    w.println("#include \"types.h\"");
                    w.println();
                }
                w.println("// ---- function prototypes ----");
                for (Res r : results.values()) {
                    if (r.sig != null) {
                        w.println(r.sig.trim().endsWith(";") ? r.sig.trim() : r.sig.trim() + ";");
                    }
                }
                w.println();
                w.println("// ---- functions ----");
                for (Res r : results.values()) {
                    w.println();
                    w.println("// ---- " + r.name + " @ " + r.addr + " ----");
                    if (r.c != null) {
                        w.print(r.c);
                        good++;
                    }
                    else {
                        w.println("// could not be decompiled: " + r.error);
                        bad++;
                    }
                }
                if (hidden > 0) {
                    w.println();
                    w.println("// " + hidden + " compiler start-up function(s) hidden (_start, _init, PLT stubs, ...).");
                }
                int missing = funcs.size() - results.size();
                if (missing > 0) {
                    w.println();
                    w.println("// " + missing + " more function(s) were not reached before the time limit.");
                }
            }
        }
        say("Decompiled " + good + " function(s), " + bad + " with errors, "
            + (funcs.size() - results.size()) + " not reached; wrote " + out.getName() + " (" + out.length() + " bytes)");
        return good > 0 ? 0 : 4;
    }
}
