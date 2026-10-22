import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.Driver;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.Set;

/**
 * JdbcBridge —— DataAgent M7 JDBC 泛用适配器的 Java 端桥。
 *
 * <p>Python 没有原生 JDBC，Oracle / SQL Server / 达梦等库往往只有 JDBC 驱动。
 * 本桥把"一次连接 + 若干只读查询/元数据"封装成一个常驻子进程，Python 侧用
 * 二进制 stdin/stdout 协议驱动它（见 {@code nl2sql/sources/jdbc.py}）。
 *
 * <h2>协议（全部走 stdin/stdout，UTF 字符串为 Java {@code DataInputStream.readUTF}）</h2>
 * <pre>
 * Python 发命令（每条命令一个 writeUTF，后跟该命令的参数字段）：
 *   connect   readUTF driverClass, readUTF url, readUTF user,
 *             readUTF password, readUTF jarPaths（File.pathSeparator 连接，可为空）
 *   test      （无参数）              SELECT 1 自检
 *   metadata  （无参数）              库表元数据
 *   query     readUTF sql, readInt limit, readInt timeoutSec
 *   close     （无参数）              关闭连接并退出（仍回一条 JSON）
 *
 * Java 回一行 JSON（UTF-8 + '\n'，字符串内控制字符转义，保证一行一条响应）：
 *   {"ok":true, ...} / {"ok":false,"error":"...","timeout":false,
 *                        "sql_state":"...","error_code":0}
 *   connect:  {"ok":true,"driver":"..."}
 *   metadata: {"ok":true,"tables":[{"name","schema","comment","row_estimate",
 *              "columns":[{"name","data_type","nullable","comment",
 *                          "is_primary_key","ordinal","generated"}],
 *              "foreign_keys":[["col","schema.table","refcol"]],
 *              "privileges":[...]}], "warnings":[...]}
 *   query:    {"ok":true,"columns":[...], "rows":[[...]], "truncated":bool,
 *              "elapsed_ms":0, "warnings":[...]}
 * </pre>
 *
 * <p>设计要点：
 * <ul>
 *   <li>用 {@link URLClassLoader} 加载驱动 jar，{@code Class.forName} 后
 *       {@code newInstance()} 得到 {@link Driver}，直接 {@code driver.connect}，
 *       不依赖 {@code DriverManager}（避免系统类加载器看不到 jar 的坑）。</li>
 *   <li>结果单元格一律转字符串，{@code NULL} 用 JSON null。</li>
 *   <li>查询最多抓 {@code limit+1} 行判断 truncated；{@code limit<=0} 时用
 *       {@link #MAX_ROWS_HARD} 兜底，防 OOM。</li>
 *   <li>超时双保险：{@code Statement.setQueryTimeout}（服务端/驱动侧）+ Python
 *       deadline（桥无响应就杀进程）；超时类 SQLException 在 JSON 里标
 *       {@code "timeout":true}。</li>
 *   <li>单表列数/总表数有上限，超出记 warnings，不让一个巨型库把响应撑爆。</li>
 * </ul>
 */
public final class JdbcBridge {

    /** 元数据最多返回的表数（超过记 warning）。 */
    private static final int MAX_TABLES = 2000;
    /** 单表最多返回的列数。 */
    private static final int MAX_COLUMNS_PER_TABLE = 500;
    /** limit<=0 时的硬上限，防一次性拉空整个结果集。 */
    private static final int MAX_ROWS_HARD = 10000;
    /** 单元格字符串长度上限，超出截断并记 warning。 */
    private static final int MAX_CELL_CHARS = 10000;
    /** 错误信息长度上限。 */
    private static final int MAX_ERROR_CHARS = 3000;

    private static DataInputStream in;
    private static OutputStream out;
    private static Connection conn;
    private static DatabaseMetaData meta;

    private JdbcBridge() {
    }

    public static void main(String[] args) throws Exception {
        // 用 FileDescriptor 直接拿 stdin/stdout：不经过 System.out 的
        // 平台默认编码与换行翻译，协议是纯二进制 UTF-8。
        in = new DataInputStream(
                new BufferedInputStream(new FileInputStream(FileDescriptor.in), 1 << 16));
        out = new BufferedOutputStream(
                new FileOutputStream(FileDescriptor.out), 1 << 16);

        while (true) {
            String cmd;
            try {
                cmd = in.readUTF();
            } catch (EOFException eof) {
                break;  // Python 侧关闭了 stdin
            } catch (IOException broken) {
                break;  // 管道断裂，Python 侧已不在
            }
            if ("close".equals(cmd)) {
                closeQuietly();
                writeLine("{\"ok\":true}");
                break;
            }
            String response;
            try {
                response = handle(cmd);
            } catch (Throwable t) {
                response = errorJson(t);
            }
            writeLine(response);
        }
        closeQuietly();
    }

    // ------------------------------------------------------------ 命令分发
    private static String handle(String cmd) throws IOException, SQLException {
        switch (cmd) {
            case "connect":
                return cmdConnect();
            case "test":
                return cmdTest();
            case "metadata":
                return cmdMetadata();
            case "query":
                return cmdQuery();
            default:
                return "{\"ok\":false,\"error\":"
                        + js("未知命令: " + clip(cmd, 200)) + ",\"timeout\":false}";
        }
    }

    // ------------------------------------------------------------ 连接
    private static String cmdConnect() throws IOException, SQLException {
        String driverClass = in.readUTF();
        String url = in.readUTF();
        String user = in.readUTF();
        String password = in.readUTF();
        String jarPaths = in.readUTF();

        if (conn != null) {
            closeQuietly();
        }

        List<URL> urls = new ArrayList<>();
        if (jarPaths != null && !jarPaths.isEmpty()) {
            for (String p : jarPaths.split(java.util.regex.Pattern.quote(File.pathSeparator))) {
                p = p.trim();
                if (p.isEmpty()) {
                    continue;
                }
                File f = new File(p);
                if (!f.isFile()) {
                    throw new SQLException("驱动 jar 不存在: " + p, "08001");
                }
                urls.add(f.toURI().toURL());
            }
        }

        // 驱动类由 URLClassLoader 从 jar 里加载；parent 用桥自己的 loader，
        // 这样 jar 放在 -cp 上或只通过 URLClassLoader 传入都能工作。
        URLClassLoader loader = new URLClassLoader(
                urls.toArray(new URL[0]), JdbcBridge.class.getClassLoader());
        Class<?> cls;
        try {
            cls = Class.forName(driverClass, true, loader);
        } catch (ClassNotFoundException e) {
            throw new SQLException(
                    "找不到 JDBC 驱动类: " + driverClass
                            + "（请检查 driver_class 与 jar_paths 是否匹配）",
                    "08001", e);
        }
        Object drvObj;
        try {
            drvObj = cls.getDeclaredConstructor().newInstance();
        } catch (ReflectiveOperationException | RuntimeException e) {
            throw new SQLException("JDBC 驱动类无法实例化: " + driverClass, "08001", e);
        }
        if (!(drvObj instanceof Driver)) {
            throw new SQLException(
                    "驱动类不是 java.sql.Driver 实现: " + driverClass, "08001");
        }

        Properties props = new Properties();
        if (user != null && !user.isEmpty()) {
            props.setProperty("user", user);
        }
        if (password != null && !password.isEmpty()) {
            props.setProperty("password", password);
        }

        Connection c;
        try {
            c = ((Driver) drvObj).connect(url, props);
        } catch (SQLException e) {
            throw e;
        }
        if (c == null) {
            throw new SQLException(
                    "JDBC 驱动不接受该 URL（driver.connect 返回 null）: " + url, "08001");
        }
        // best-effort 只读：不是所有驱动都支持，失败不回滚连接
        try {
            c.setReadOnly(true);
        } catch (SQLException ignore) {
            // 只读语义主要靠 Python 侧语句校验 + 数据库账号权限
        }

        conn = c;
        try {
            meta = c.getMetaData();
        } catch (SQLException e) {
            closeQuietly();
            throw e;
        }
        return "{\"ok\":true,\"driver\":" + js(driverName()) + "}";
    }

    private static String driverName() {
        try {
            if (meta != null) {
                return meta.getDriverName() + " " + meta.getDriverVersion();
            }
        } catch (SQLException ignore) {
            // 元数据不可用不影响连接结果
        }
        return "";
    }

    private static String cmdTest() {
        if (conn == null) {
            return simpleError("尚未连接（请先执行 connect）");
        }
        boolean ok = false;
        String detail = "";
        try {
            ok = conn.isValid(10);
        } catch (SQLException e) {
            detail = safeMessage(e);
        }
        if (!ok) {
            try (Statement st = conn.createStatement()) {
                try {
                    st.setQueryTimeout(10);
                } catch (SQLException ignore) {
                    // 老驱动可能不支持，自检不因此失败
                }
                try (ResultSet rs = st.executeQuery("SELECT 1")) {
                    ok = rs.next();
                }
            } catch (SQLException e) {
                detail = safeMessage(e);
            }
        }
        if (ok) {
            return "{\"ok\":true}";
        }
        return simpleError("连接自检失败" + (detail.isEmpty() ? "" : ": " + clip(detail, 1000)));
    }

    // ------------------------------------------------------------ 元数据
    private static String cmdMetadata() throws SQLException {
        if (conn == null) {
            return simpleError("尚未连接（请先执行 connect）");
        }
        DatabaseMetaData md = conn.getMetaData();
        List<String> warnings = new ArrayList<>();

        String catalog = null;
        String schemaPattern = null;
        try {
            catalog = conn.getCatalog();
        } catch (SQLException ignore) {
            // 驱动不支持 catalog
        }
        try {
            schemaPattern = conn.getSchema();
        } catch (SQLException | AbstractMethodError ignore) {
            // JDBC 4.0 驱动可能没有 getSchema
        }

        LinkedHashMap<String, TableInfo> tables = new LinkedHashMap<>();
        try {
            loadTables(md, catalog, schemaPattern, tables);
            if (tables.isEmpty() && (catalog != null || schemaPattern != null)) {
                // 某些驱动对 catalog/schema 模式很挑剔，退回全库扫描
                loadTables(md, null, null, tables);
            }
        } catch (SQLException e) {
            return errorJson(e);
        }
        if (tables.isEmpty()) {
            // 也把视图算上再试一次（少数驱动把类型过滤当硬条件）
            try {
                loadTablesAllTypes(md, catalog, schemaPattern, tables);
                if (tables.isEmpty() && (catalog != null || schemaPattern != null)) {
                    loadTablesAllTypes(md, null, null, tables);
                }
            } catch (SQLException e) {
                return errorJson(e);
            }
        }

        Map<String, List<String>> privileges = loadPrivileges(md);

        StringBuilder sb = new StringBuilder(1 << 16);
        sb.append("{\"ok\":true,\"driver\":").append(js(driverName()));
        sb.append(",\"tables\":[");
        int ti = 0;
        for (TableInfo t : tables.values()) {
            if (ti > 0) {
                sb.append(',');
            }
            ti++;
            appendTable(sb, md, t, privileges, warnings);
        }
        sb.append("],\"warnings\":[").append(jsonArray(warnings)).append("]}");
        return sb.toString();
    }

    private static void appendTable(StringBuilder sb, DatabaseMetaData md, TableInfo t,
                                    Map<String, List<String>> privileges,
                                    List<String> warnings) {
        List<ColumnInfo> columns = new ArrayList<>();
        try {
            loadColumns(md, t, columns);
        } catch (SQLException e) {
            warnings.add("表 " + qualified(t) + " 取列失败: " + clip(safeMessage(e), 300));
        }
        if (columns.isEmpty()) {
            warnings.add("表 " + qualified(t) + " 未取到列信息");
        }
        Collections.sort(columns, (a, b) -> Integer.compare(a.ordinal, b.ordinal));

        Set<String> primaryKeys = new HashSet<>();
        try {
            loadPrimaryKeys(md, t, primaryKeys);
        } catch (SQLException e) {
            warnings.add("表 " + qualified(t) + " 取主键失败: " + clip(safeMessage(e), 300));
        }

        List<String[]> foreignKeys = new ArrayList<>();
        try {
            loadForeignKeys(md, t, foreignKeys);
        } catch (SQLException e) {
            warnings.add("表 " + qualified(t) + " 取外键失败: " + clip(safeMessage(e), 300));
        }

        sb.append("{\"name\":").append(js(t.name));
        sb.append(",\"schema\":").append(js(t.schema));
        sb.append(",\"comment\":").append(js(t.comment));
        sb.append(",\"row_estimate\":null");
        sb.append(",\"columns\":[");
        for (int i = 0; i < columns.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            ColumnInfo c = columns.get(i);
            sb.append("{\"name\":").append(js(c.name));
            sb.append(",\"data_type\":").append(js(c.dataType));
            sb.append(",\"nullable\":").append(c.nullable);
            sb.append(",\"comment\":").append(js(c.comment));
            sb.append(",\"is_primary_key\":").append(primaryKeys.contains(c.name));
            sb.append(",\"ordinal\":").append(c.ordinal);
            sb.append(",\"generated\":").append(c.generated);
            sb.append('}');
        }
        sb.append("],\"foreign_keys\":[");
        for (int i = 0; i < foreignKeys.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            String[] fk = foreignKeys.get(i);
            sb.append('[').append(js(fk[0])).append(',').append(js(fk[1]))
                    .append(',').append(js(fk[2])).append(']');
        }
        List<String> priv = privileges.getOrDefault(keyOf(t.schema, t.name), Collections.emptyList());
        sb.append("],\"privileges\":[").append(jsonArray(priv)).append("]}");
    }

    private static void loadTables(DatabaseMetaData md, String catalog, String schemaPattern,
                                   LinkedHashMap<String, TableInfo> out) throws SQLException {
        try (ResultSet rs = md.getTables(catalog, schemaPattern, "%",
                new String[]{"TABLE", "VIEW"})) {
            collectTables(rs, out);
        }
    }

    private static void loadTablesAllTypes(DatabaseMetaData md, String catalog, String schemaPattern,
                                           LinkedHashMap<String, TableInfo> out) throws SQLException {
        try (ResultSet rs = md.getTables(catalog, schemaPattern, "%", null)) {
            collectTables(rs, out);
        }
    }

    private static void collectTables(ResultSet rs, LinkedHashMap<String, TableInfo> out)
            throws SQLException {
        while (rs.next()) {
            if (out.size() >= MAX_TABLES) {
                return;
            }
            String name = getStr(rs, "TABLE_NAME");
            if (name == null || name.isEmpty()) {
                continue;
            }
            String schema = getStr(rs, "TABLE_SCHEM");
            String key = keyOf(schema, name);
            if (out.containsKey(key)) {
                continue;
            }
            TableInfo t = new TableInfo();
            t.catalog = getStr(rs, "TABLE_CAT");
            t.schema = schema;
            t.name = name;
            t.type = getStr(rs, "TABLE_TYPE");
            t.comment = getStr(rs, "REMARKS");
            out.put(key, t);
        }
    }

    private static void loadColumns(DatabaseMetaData md, TableInfo t, List<ColumnInfo> out)
            throws SQLException {
        String namePattern = escapeSearchPattern(md, t.name);
        try (ResultSet rs = md.getColumns(t.catalog, t.schema, namePattern, "%")) {
            collectColumns(rs, out);
        }
        if (out.isEmpty() && (t.schema != null || t.catalog != null)) {
            try (ResultSet rs = md.getColumns(null, null, namePattern, "%")) {
                collectColumns(rs, out);
            }
        }
        if (out.isEmpty()) {
            // 最后兜底：全库列扫描 + 名称精确比对（慢，但只在正常路径失败时走到）
            try (ResultSet rs = md.getColumns(null, null, "%", "%")) {
                int guard = 0;
                while (rs.next() && guard++ < 100000) {
                    String tName = getStr(rs, "TABLE_NAME");
                    if (tName == null || !tName.equals(t.name)) {
                        continue;
                    }
                    String tSchema = getStr(rs, "TABLE_SCHEM");
                    if (t.schema != null && tSchema != null && !t.schema.equals(tSchema)) {
                        continue;
                    }
                    addColumn(rs, out);
                }
            }
        }
    }

    private static void collectColumns(ResultSet rs, List<ColumnInfo> out) throws SQLException {
        while (rs.next()) {
            if (out.size() >= MAX_COLUMNS_PER_TABLE) {
                return;
            }
            addColumn(rs, out);
        }
    }

    private static void addColumn(ResultSet rs, List<ColumnInfo> out) {
        String name;
        try {
            name = rs.getString("COLUMN_NAME");
        } catch (SQLException e) {
            return;
        }
        if (name == null || name.isEmpty()) {
            return;
        }
        for (ColumnInfo c : out) {
            if (c.name.equals(name)) {
                return;  // 去重（全库扫描兜底时可能重复）
            }
        }
        ColumnInfo c = new ColumnInfo();
        c.name = name;
        String typeName = getStr(rs, "TYPE_NAME");
        if (typeName != null && !typeName.isEmpty()) {
            c.dataType = typeName.toUpperCase(Locale.ROOT);
        } else {
            int code = 0;
            try {
                code = rs.getInt("DATA_TYPE");
            } catch (SQLException ignore) {
                // 极端情况没有 DATA_TYPE
            }
            c.dataType = "TYPE_" + code;
        }
        boolean nullable = true;
        try {
            nullable = rs.getInt("NULLABLE") != DatabaseMetaData.columnNoNulls;
        } catch (SQLException ignore) {
            // 缺省按可空处理更安全（语义层只是提示）
        }
        c.nullable = nullable;
        c.comment = getStr(rs, "REMARKS");
        c.generated = "YES".equalsIgnoreCase(getStr(rs, "IS_GENERATEDCOLUMN"));
        int ordinal = out.size();
        try {
            int pos = rs.getInt("ORDINAL_POSITION");
            if (pos > 0) {
                ordinal = pos - 1;  // 契约里 ordinal 是 0 基
            }
        } catch (SQLException ignore) {
            // 没有序号就按返回顺序
        }
        c.ordinal = ordinal;
        out.add(c);
    }

    private static void loadPrimaryKeys(DatabaseMetaData md, TableInfo t, Set<String> out)
            throws SQLException {
        try (ResultSet rs = md.getPrimaryKeys(t.catalog, t.schema, t.name)) {
            while (rs.next()) {
                String col = getStr(rs, "COLUMN_NAME");
                if (col != null && !col.isEmpty()) {
                    out.add(col);
                }
            }
        }
        if (out.isEmpty()) {
            try (ResultSet rs = md.getPrimaryKeys(null, null, t.name)) {
                while (rs.next()) {
                    String col = getStr(rs, "COLUMN_NAME");
                    if (col != null && !col.isEmpty()) {
                        out.add(col);
                    }
                }
            }
        }
    }

    private static void loadForeignKeys(DatabaseMetaData md, TableInfo t, List<String[]> out)
            throws SQLException {
        try (ResultSet rs = md.getImportedKeys(t.catalog, t.schema, t.name)) {
            collectForeignKeys(rs, out);
        }
        if (out.isEmpty()) {
            try (ResultSet rs = md.getImportedKeys(null, null, t.name)) {
                collectForeignKeys(rs, out);
            }
        }
    }

    private static void collectForeignKeys(ResultSet rs, List<String[]> out) throws SQLException {
        while (rs.next()) {
            String col = getStr(rs, "FKCOLUMN_NAME");
            String refTable = getStr(rs, "PKTABLE_NAME");
            String refSchema = getStr(rs, "PKTABLE_SCHEM");
            String refCol = getStr(rs, "PKCOLUMN_NAME");
            if (col == null || col.isEmpty() || refTable == null || refTable.isEmpty()) {
                continue;
            }
            String ref = (refSchema == null || refSchema.isEmpty())
                    ? refTable : refSchema + "." + refTable;
            out.add(new String[]{col, ref, refCol == null ? "" : refCol});
        }
    }

    private static Map<String, List<String>> loadPrivileges(DatabaseMetaData md) {
        Map<String, List<String>> out = new HashMap<>();
        try (ResultSet rs = md.getTablePrivileges(null, null, "%")) {
            int guard = 0;
            while (rs.next() && guard++ < 100000) {
                String table = getStr(rs, "TABLE_NAME");
                if (table == null || table.isEmpty()) {
                    continue;
                }
                String privilege = getStr(rs, "PRIVILEGE");
                if (privilege == null || privilege.isEmpty()) {
                    continue;
                }
                String schema = getStr(rs, "TABLE_SCHEM");
                out.computeIfAbsent(keyOf(schema, table), k -> new ArrayList<>())
                        .add(privilege.toLowerCase(Locale.ROOT));
            }
        } catch (Throwable ignore) {
            // 不少驱动不支持 getTablePrivileges（SQLFeatureNotSupportedException），
            // 或视图权限查询需要额外权限；这不是元数据抽取的必需项。
        }
        for (List<String> v : out.values()) {
            Collections.sort(v);
        }
        return out;
    }

    // ------------------------------------------------------------ 查询
    private static String cmdQuery() throws IOException {
        String sql = in.readUTF();
        int limit = in.readInt();
        int timeoutSec = in.readInt();

        if (conn == null) {
            return simpleError("尚未连接（请先执行 connect）");
        }
        if (limit <= 0) {
            limit = MAX_ROWS_HARD;
        }
        long startedAt = System.nanoTime();
        List<String> warnings = new ArrayList<>();

        try (Statement st = conn.createStatement()) {
            if (timeoutSec > 0) {
                try {
                    st.setQueryTimeout(timeoutSec);
                } catch (SQLException e) {
                    warnings.add("驱动不支持 setQueryTimeout，仅依赖客户端超时");
                }
            }
            boolean hasResult;
            try {
                hasResult = st.execute(sql);
            } catch (SQLException e) {
                return errorJson(e);
            }
            if (!hasResult) {
                // Python 侧只允许 SELECT/WITH，走到这里说明驱动/方言行为异常
                return simpleError(
                        "JDBC 适配器只支持只读查询：该语句没有返回结果集");
            }

            try (ResultSet rs = st.getResultSet()) {
                ResultSetMetaData rsmd = rs.getMetaData();
                int columnCount = rsmd.getColumnCount();
                List<String> columns = new ArrayList<>(columnCount);
                for (int i = 1; i <= columnCount; i++) {
                    String label = rsmd.getColumnLabel(i);
                    if (label == null || label.isEmpty()) {
                        label = rsmd.getColumnName(i);
                    }
                    columns.add(label == null ? "" : label);
                }

                List<String[]> rows = new ArrayList<>();
                boolean truncated = false;
                boolean cellTruncated = false;
                while (rs.next()) {
                    if (rows.size() >= limit) {
                        truncated = true;
                        break;
                    }
                    String[] row = new String[columnCount];
                    for (int i = 1; i <= columnCount; i++) {
                        String value = rs.getString(i);
                        if (rs.wasNull()) {
                            row[i - 1] = null;
                            continue;
                        }
                        if (value.length() > MAX_CELL_CHARS) {
                            value = value.substring(0, MAX_CELL_CHARS) + "…（已截断）";
                            cellTruncated = true;
                        }
                        row[i - 1] = value;
                    }
                    rows.add(row);
                }
                if (cellTruncated) {
                    warnings.add("部分单元格超过 " + MAX_CELL_CHARS + " 字符，已截断");
                }
                if (truncated) {
                    warnings.add("结果被截断，仅返回前 " + limit + " 行");
                }
                long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000L;

                StringBuilder sb = new StringBuilder(1 << 14);
                sb.append("{\"ok\":true,\"columns\":[");
                for (int i = 0; i < columns.size(); i++) {
                    if (i > 0) {
                        sb.append(',');
                    }
                    sb.append(js(columns.get(i)));
                }
                sb.append("],\"rows\":[");
                for (int r = 0; r < rows.size(); r++) {
                    if (r > 0) {
                        sb.append(',');
                    }
                    sb.append('[');
                    String[] row = rows.get(r);
                    for (int c = 0; c < row.length; c++) {
                        if (c > 0) {
                            sb.append(',');
                        }
                        sb.append(js(row[c]));
                    }
                    sb.append(']');
                }
                sb.append("],\"truncated\":").append(truncated);
                sb.append(",\"elapsed_ms\":").append(elapsedMs);
                sb.append(",\"warnings\":[").append(jsonArray(warnings)).append("]}");
                return sb.toString();
            }
        } catch (SQLException e) {
            return errorJson(e);
        }
    }

    // ------------------------------------------------------------ 错误
    /** 查询/元数据失败：带 SQLState、errorCode、timeout 标记。 */
    private static String errorJson(Throwable t) {
        boolean timeout = isTimeout(t);
        SQLException se = findSqlException(t);
        String state = null;
        int code = 0;
        if (se != null) {
            state = se.getSQLState();
            code = se.getErrorCode();
        }
        String message = describe(t);
        if (state != null || code != 0) {
            message += "（SQLState=" + (state == null ? "" : state)
                    + ", errorCode=" + code + "）";
        }
        StringBuilder b = new StringBuilder(256);
        b.append("{\"ok\":false,\"error\":").append(js(clip(message, MAX_ERROR_CHARS)));
        b.append(",\"timeout\":").append(timeout);
        b.append(",\"sql_state\":").append(js(state));
        b.append(",\"error_code\":").append(code);
        b.append('}');
        return b.toString();
    }

    private static String simpleError(String message) {
        return "{\"ok\":false,\"error\":" + js(clip(message, MAX_ERROR_CHARS))
                + ",\"timeout\":false}";
    }

    private static String describe(Throwable t) {
        StringBuilder b = new StringBuilder();
        Throwable cur = t;
        Set<Throwable> seen = Collections.newSetFromMap(new IdentityHashMap<>());
        int depth = 0;
        while (cur != null && depth++ < 6 && seen.add(cur)) {
            String msg = cur.getMessage();
            if (msg != null && !msg.trim().isEmpty()) {
                if (b.length() > 0) {
                    b.append(" ← ");
                }
                b.append(cur.getClass().getSimpleName()).append(": ").append(msg.trim());
            }
            cur = cur.getCause();
        }
        if (b.length() == 0) {
            b.append(t.getClass().getName());
        }
        return b.toString();
    }

    private static String safeMessage(Throwable t) {
        String msg = t.getMessage();
        return msg == null || msg.trim().isEmpty() ? t.getClass().getSimpleName() : msg.trim();
    }

    private static SQLException findSqlException(Throwable t) {
        SQLException first = null;
        Throwable cur = t;
        Set<Throwable> seen = Collections.newSetFromMap(new IdentityHashMap<>());
        while (cur != null && seen.add(cur)) {
            if (cur instanceof SQLException) {
                SQLException se = (SQLException) cur;
                if (first == null) {
                    first = se;
                }
                if (se.getSQLState() != null || se.getErrorCode() != 0) {
                    return se;
                }
                SQLException next = se.getNextException();
                if (next != null && !seen.contains(next)) {
                    cur = next;
                    continue;
                }
            }
            cur = cur.getCause();
        }
        return first;
    }

    private static boolean isTimeout(Throwable t) {
        Throwable cur = t;
        Set<Throwable> seen = Collections.newSetFromMap(new IdentityHashMap<>());
        while (cur != null && seen.add(cur)) {
            if (cur instanceof SQLException) {
                SQLException se = (SQLException) cur;
                String state = se.getSQLState();
                if (state != null) {
                    // 57xxx: PostgreSQL/DB2 "operator intervention"（含 57014 cancel）
                    // HYT00/HYT01: ODBC 超时；S1T00: SQL Server 客户端超时
                    if (state.startsWith("57") || state.equals("HYT00")
                            || state.equals("HYT01") || state.equals("S1T00")) {
                        return true;
                    }
                }
                SQLException next = se.getNextException();
                if (next != null && !seen.contains(next)) {
                    cur = next;
                    continue;
                }
            }
            String msg = cur.getMessage();
            if (msg != null) {
                String low = msg.toLowerCase(Locale.ROOT);
                if (low.contains("timeout") || low.contains("timed out")
                        || low.contains("interrupted") || low.contains("cancel")
                        || low.contains("max_execution_time")) {
                    return true;
                }
            }
            cur = cur.getCause();
        }
        return false;
    }

    // ------------------------------------------------------------ 杂项
    private static void closeQuietly() {
        Connection c = conn;
        conn = null;
        meta = null;
        if (c != null) {
            try {
                c.close();
            } catch (Throwable ignore) {
                // 关闭失败没有恢复手段
            }
        }
    }

    private static void writeLine(String line) throws IOException {
        out.write(line.getBytes(StandardCharsets.UTF_8));
        out.write('\n');
        out.flush();
    }

    private static String getStr(ResultSet rs, String column) {
        try {
            return rs.getString(column);
        } catch (SQLException e) {
            return null;
        }
    }

    /** 转义 LIKE 模式里的 % 与 _（用驱动的 search escape，没有就用反斜杠）。 */
    private static String escapeSearchPattern(DatabaseMetaData md, String name) {
        String esc = "\\";
        try {
            String e = md.getSearchStringEscape();
            if (e != null && !e.isEmpty()) {
                esc = e;
            }
        } catch (SQLException ignore) {
            // 用默认反斜杠
        }
        if (esc.isEmpty()) {
            return name;
        }
        return name.replace(esc, esc + esc)
                .replace("_", esc + "_")
                .replace("%", esc + "%");
    }

    private static String keyOf(String schema, String name) {
        return (schema == null ? "" : schema) + "\u0000" + (name == null ? "" : name);
    }

    private static String qualified(TableInfo t) {
        return (t.schema == null || t.schema.isEmpty()) ? t.name : t.schema + "." + t.name;
    }

    private static String clip(String s, int max) {
        if (s == null) {
            return "";
        }
        return s.length() <= max ? s : s.substring(0, max) + "…";
    }

    /** 单行 JSON 字符串字面量（控制字符转义，保证响应不跨行）。 */
    private static String js(String s) {
        if (s == null) {
            return "null";
        }
        StringBuilder b = new StringBuilder(s.length() + 2);
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    b.append("\\\"");
                    break;
                case '\\':
                    b.append("\\\\");
                    break;
                case '\b':
                    b.append("\\b");
                    break;
                case '\f':
                    b.append("\\f");
                    break;
                case '\n':
                    b.append("\\n");
                    break;
                case '\r':
                    b.append("\\r");
                    break;
                case '\t':
                    b.append("\\t");
                    break;
                default:
                    if (c < 0x20 || c == 0x7f) {
                        b.append(String.format("\\u%04x", (int) c));
                    } else {
                        b.append(c);
                    }
            }
        }
        b.append('"');
        return b.toString();
    }

    private static String jsonArray(List<String> values) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                b.append(',');
            }
            b.append(js(values.get(i)));
        }
        return b.toString();
    }

    // ------------------------------------------------------------ 内部类型
    private static final class TableInfo {
        String catalog;
        String schema;
        String name;
        String type;
        String comment;
    }

    private static final class ColumnInfo {
        String name;
        String dataType;
        boolean nullable = true;
        String comment;
        boolean generated;
        int ordinal;
    }
}
