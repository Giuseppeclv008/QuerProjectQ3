#include "mas/store/RawInput.hpp"
#include "mas/domain/RowParse.hpp"
#include "mas/store/CsvRawReader.hpp"
#include "mas/store/DuckDbExec.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <set>
#include <stdexcept>
#include <vector>

namespace mas {

namespace {

std::string lower_ext(const std::string& path) {
    std::string e = std::filesystem::path(path).extension().string();
    std::transform(e.begin(), e.end(), e.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return e;
}

// Column names go into the SELECT list as identifiers; double any '"'.
std::string quote_ident(const std::string& name) {
    std::string q = "\"";
    for (char c : name) {
        if (c == '"') q += '"';
        q += c;
    }
    return q + "\"";
}

// The timestamp in the pool's own spelling, "2026-02-27T16:00:00.000",
// whether the file typed it (Parquet TIMESTAMP) or spelled it differently
// (DuckDB's own JSON writer emits "2026-02-27 16:00:00"). Anything that is not
// a timestamp at all passes through as text, as an odd CSV cell would.
std::string ts_expr(const std::string& id) {
    return "COALESCE(strftime(TRY_CAST(" + id +
           " AS TIMESTAMP), '%Y-%m-%dT%H:%M:%S.%g'), CAST(" + id + " AS VARCHAR))";
}

} // namespace

RawFormat raw_format_for(const std::string& path) {
    const std::string e = lower_ext(path);
    if (e == ".parquet") return RawFormat::Parquet;
    if (e == ".json" || e == ".jsonl" || e == ".ndjson") return RawFormat::Json;
    return RawFormat::Csv;
}

const char* raw_format_name(RawFormat f) {
    switch (f) {
        case RawFormat::Parquet: return "parquet";
        case RawFormat::Json: return "json";
        case RawFormat::Csv: break;
    }
    return "csv";
}

struct TabularRawReader::Impl {
    std::unique_ptr<duckdb::DuckDB> db;
    std::unique_ptr<duckdb::Connection> con;
    std::unique_ptr<duckdb::QueryResult> result;
    std::unique_ptr<duckdb::DataChunk> chunk;
    duckdb::idx_t row = 0;
    bool open = false;
    bool done = false;
    std::size_t skipped = 0;
    std::size_t out_of_order = 0;
    std::string last_ts;
    std::string header_error;
    std::string path;
    std::vector<std::string> fields;
};

TabularRawReader::TabularRawReader(const std::string& path, RawFormat format)
    : impl_(std::make_unique<Impl>()) {
    Impl& m = *impl_;
    m.path = path;
    std::error_code ec;
    if (!std::filesystem::is_regular_file(path, ec)) return;   // like ifstream

    const std::string lit = "'" + sql_quote(path) + "'";
    const std::string probe = format == RawFormat::Parquet
        ? "read_parquet(" + lit + ")"
        : "read_json_auto(" + lit + ")";
    try {
        m.db = std::make_unique<duckdb::DuckDB>(nullptr);   // in-memory
        m.con = std::make_unique<duckdb::Connection>(*m.db);

        auto cols = query_or_throw(*m.con, "DESCRIBE SELECT * FROM " + probe);
        std::vector<std::string> got;
        for (duckdb::idx_t i = 0; i < cols->RowCount(); ++i)
            got.push_back(cols->GetValue(0, i).ToString());

        const auto want = CsvRawReader::expected_header();
        const std::set<std::string> want_set(want.begin(), want.end());
        std::set<std::string> got_set;
        for (const auto& name : got) {
            if (!want_set.count(name)) {
                m.header_error = path + ": unexpected column '" + name + "'";
                return;
            }
            got_set.insert(name);
        }
        for (const auto& name : want) {
            if (!got_set.count(name)) {
                m.header_error = path + ": missing column '" + name + "'";
                return;
            }
        }
        if (got.size() != want.size()) {   // duplicate names
            m.header_error = path + ": has " + std::to_string(got.size()) +
                             " columns, expected " + std::to_string(want.size());
            return;
        }

        std::string source;
        if (format == RawFormat::Parquet) {
            source = "read_parquet(" + lit + ")";
        } else {
            std::string spec;
            for (const auto& name : want)
                spec += (spec.empty() ? "" : ", ") + std::string("'") +
                        sql_quote(name) + "': 'VARCHAR'";
            source = "read_json(" + lit + ", columns = {" + spec + "})";
        }

        std::string select;
        for (std::size_t i = 0; i < want.size(); ++i) {
            const std::string id = quote_ident(want[i]);
            if (i) select += ", ";
            select += i == 0 ? ts_expr(id) : "CAST(" + id + " AS VARCHAR)";
        }
        // Streamed, not materialized: a day-file is 86,400 rows x 109 cells.
        m.result = m.con->SendQuery("SELECT " + select + " FROM " + source);
        if (m.result->HasError())
            throw std::runtime_error(m.result->GetError());
    } catch (const std::exception& e) {
        m.header_error = path + ": " + e.what();
        return;
    }
    m.fields.resize(1 + NUM_HEADS * 3);
    m.open = true;
}

TabularRawReader::~TabularRawReader() = default;

bool TabularRawReader::is_open() const { return impl_->open; }

std::size_t TabularRawReader::skipped() const { return impl_->skipped; }

std::size_t TabularRawReader::out_of_order() const { return impl_->out_of_order; }

const std::string& TabularRawReader::header_error() const {
    return impl_->header_error;
}

bool TabularRawReader::next(RawRow& out) {
    Impl& m = *impl_;
    if (!m.open || m.done) return false;
    for (;;) {
        if (!m.chunk || m.row >= m.chunk->size()) {
            m.chunk = m.result->Fetch();
            m.row = 0;
            if (m.result->HasError())
                throw std::runtime_error(m.path + ": " + m.result->GetError());
            if (!m.chunk || m.chunk->size() == 0) {
                m.done = true;
                return false;
            }
        }
        const duckdb::idx_t r = m.row++;
        for (std::size_t c = 0; c < m.fields.size(); ++c) {
            const duckdb::Value v = m.chunk->GetValue(c, r);
            // NULL becomes "", which stod rejects: an empty CSV cell and a
            // null JSON/Parquet value are the same malformed row.
            m.fields[c] = v.IsNull() ? std::string() : v.ToString();
        }
        if (parse_row_fields(m.fields, out) != RowParse::Ok) {
            ++m.skipped;
            continue;
        }
        if (!m.last_ts.empty() && out.ts <= m.last_ts) ++m.out_of_order;
        m.last_ts = out.ts;
        return true;
    }
}

std::unique_ptr<RawReader> open_raw_reader(const std::string& path) {
    const RawFormat f = raw_format_for(path);
    if (f == RawFormat::Csv) return std::make_unique<CsvRawReader>(path);
    return std::make_unique<TabularRawReader>(path, f);
}

} // namespace mas
