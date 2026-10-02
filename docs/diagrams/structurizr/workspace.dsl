workspace "Cap-event cleaning" {

    model {
        plc = softwareSystem "AROL PLC" "raw telemetry" "External"

        cleaning = softwareSystem "Cap-event cleaning system" {
            clean       = container "clean"           "one day-file"        "C++ CLI"
            monolith    = container "mas_monolith"    "T threads"           "C++ CLI"
            cuda        = container "CUDA cleaner"    "GPU cleaning engine" "C++ / CUDA"
            coordinator = container "mas_coordinator" "dispatch, liveness"  "C++ CLI"
            worker      = container "mas_worker x N"  "cleaning agent"      "C++ CLI"
            merge       = container "mas_merge"       "unifies stores"      "C++ CLI"
            exporter    = container "mas_export"      "store to Parquet"    "C++ CLI"
            arol        = container "arol"            "analytics, reports"  "Python"
            store       = container "DuckDB store"    "cap_events table"    "DuckDB" "Database"
        }

        plc -> clean       "day-files (CSV)"
        plc -> monolith    "day-files (CSV)"
        plc -> coordinator "day-files (CSV)"

        monolith -> cuda    "engine=cuda"
        coordinator -> worker "ZeroMQ"

        clean   -> store "writes events"
        monolith -> store "writes events"
        worker  -> store "writes events"
        merge   -> store "merges into"
        exporter -> store "reads"
        arol    -> store "queries"
    }

    views {
        container cleaning "Containers" {
            include *
            autoLayout tb 150 100
        }

        styles {
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "External" {
                background #999999
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Database" {
                shape Cylinder
            }
        }
    }
}
