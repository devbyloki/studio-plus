#include "native/mod_files.h"

#include "Engine/Resource/cas_codec.h"

#include <windows.h>
#include <bcrypt.h>

#include <array>
#include <fstream>
#include <map>
#include <stdexcept>

#pragma comment(lib, "bcrypt.lib")

namespace studio::native {
namespace {
class Out {
public:
    std::vector<std::byte> bytes;
    void raw(const void* p, std::size_t n) {
        const auto* b = static_cast<const std::byte*>(p);
        bytes.insert(bytes.end(), b, b + n);
    }
    template <class T> void num(T v) { raw(&v, sizeof(T)); }
    void str32(const std::string& s) { num<std::uint32_t>(static_cast<std::uint32_t>(s.size())); raw(s.data(), s.size()); }
    void cstr(const std::string& s) { raw(s.data(), s.size()); num<std::uint8_t>(0); }
    void dotnet(const std::string& s) {  // 7-bit encoded length prefix
        auto n = static_cast<std::uint32_t>(s.size());
        while (n >= 0x80) { num<std::uint8_t>(static_cast<std::uint8_t>(n | 0x80)); n >>= 7; }
        num<std::uint8_t>(static_cast<std::uint8_t>(n));
        raw(s.data(), s.size());
    }
    void bytes_of(const std::vector<std::byte>& v) { bytes.insert(bytes.end(), v.begin(), v.end()); }
};

void save(const std::filesystem::path& path, const std::vector<std::byte>& bytes) {
    // Written next to the target first, so a failed write never leaves half a file in its place.
    auto temp = path;
    temp += L".partial";
    {
        std::ofstream out(temp, std::ios::binary | std::ios::trunc);
        if (!out) throw std::runtime_error("cannot create the output file");
        out.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
        if (!out) throw std::runtime_error("writing the output file failed");
    }
    std::error_code ec;
    std::filesystem::rename(temp, path, ec);
    if (ec) {
        std::filesystem::remove(temp, ec);
        throw std::runtime_error("cannot replace the output file (is it open somewhere?)");
    }
}

std::array<std::byte, 20> sha1(const std::vector<std::byte>& data) {
    std::array<std::byte, 20> out{};
    BCRYPT_ALG_HANDLE alg = nullptr;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA1_ALGORITHM, nullptr, 0) != 0) throw std::runtime_error("SHA-1 is not available");
    const NTSTATUS status = BCryptHash(alg, nullptr, 0, reinterpret_cast<PUCHAR>(const_cast<std::byte*>(data.data())),
                                       static_cast<ULONG>(data.size()), reinterpret_cast<PUCHAR>(out.data()), 20);
    BCryptCloseAlgorithmProvider(alg, 0);
    if (status != 0) throw std::runtime_error("SHA-1 failed");
    return out;
}

std::uint32_t kind_code(const ModResource& r) { return static_cast<std::uint32_t>(r.kind) | (r.added ? 0x100u : 0u); }
} // namespace

std::uint32_t frosty_hash(std::string_view text) {
    std::uint32_t h = 5381;
    for (char c : text) h = (h * 33) ^ static_cast<std::uint8_t>(c);
    return h;
}

void write_fbproject(const std::filesystem::path& path, const ModInfo& info, const std::vector<ModResource>& resources) {
    Out o;
    o.raw("RSPROJT1", 8);
    o.num<std::uint32_t>(2);
    o.str32("Skate");
    o.num<std::uint32_t>(info.head);
    for (const auto* s : {&info.title, &info.author, &info.category, &info.version, &info.description, &info.link}) o.str32(*s);
    for (int i = 0; i < 40; ++i) o.num<std::uint8_t>(0);
    o.num<std::uint32_t>(static_cast<std::uint32_t>(resources.size()));
    for (const auto& r : resources) {
        o.num<std::uint32_t>(kind_code(r));
        o.str32(r.name);
        o.str32(r.user_data);
        const bool res = r.kind == ModResource::Kind::res, chunk = r.kind == ModResource::Kind::chunk;
        o.num<std::uint32_t>(res ? r.res_type : 0);
        o.num<std::uint64_t>(res ? r.res_rid : 0);
        o.num<std::uint64_t>(res ? r.res_meta.size() : 0);
        if (res) o.bytes_of(r.res_meta);
        const dingosdk::frostbite::Guid zero{};
        o.raw(r.kind == ModResource::Kind::res ? zero.bytes.data() : r.id.bytes.data(), 16);
        o.raw(chunk ? r.id.bytes.data() : zero.bytes.data(), 16);
        for (int i = 0; i < 12; ++i) o.num<std::uint8_t>(0);
        o.num<std::uint64_t>(chunk ? r.data.size() : 0);
        o.num<std::int32_t>(-1);
        for (const auto* list : {&r.bundles, &r.superbundles, &r.links}) {
            o.num<std::uint32_t>(static_cast<std::uint32_t>(list->size()));
            for (const auto& s : *list) o.str32(s);
        }
        o.num<std::uint8_t>(0);
        o.num<std::uint64_t>(r.data.size());
        o.bytes_of(r.data);
    }
    save(path, o.bytes);
}

void write_fbmod(const std::filesystem::path& path, const ModInfo& info, const std::vector<ModResource>& resources,
                 const std::filesystem::path& game_root) {
    // Payloads first, so the table can name them; identical payloads are stored once.
    std::vector<std::vector<std::byte>> payloads;
    std::map<std::array<std::byte, 20>, int> by_hash;
    std::vector<int> index_of;
    std::vector<std::array<std::byte, 20>> resource_hash;
    bool oodle = !game_root.empty();
    for (const auto& r : resources) {
        std::vector<std::byte> packed;
        if (oodle) {
            try {
                packed = dingosdk::frostbite::encode_cas(r.data, {.gameRoot = game_root});
            } catch (const std::exception&) {
                oodle = false;
            }
        }
        if (!oodle)
            packed = dingosdk::frostbite::encode_cas(r.data, {.compression = dingosdk::frostbite::CasCompression::raw});
        const auto h = sha1(packed);
        resource_hash.push_back(h);
        auto it = by_hash.find(h);
        if (it == by_hash.end()) {
            it = by_hash.emplace(h, static_cast<int>(payloads.size())).first;
            payloads.push_back(std::move(packed));
        }
        index_of.push_back(it->second);
    }

    Out table;
    table.dotnet("Skate");
    table.num<std::uint32_t>(info.head);
    for (const auto* s : {&info.title, &info.author, &info.category, &info.version, &info.description, &info.link}) table.cstr(*s);
    table.num<std::int32_t>(static_cast<std::int32_t>(resources.size() + 5));
    for (const char* embedded : {"Icon", "Screenshot0", "Screenshot1", "Screenshot2", "Screenshot3"}) {
        table.num<std::uint8_t>(0);
        table.num<std::int32_t>(-1);
        table.cstr(embedded);
        table.num<std::int32_t>(0);
    }
    for (std::size_t i = 0; i < resources.size(); ++i) {
        const auto& r = resources[i];
        table.num<std::uint8_t>(static_cast<std::uint8_t>(r.kind));
        table.num<std::int32_t>(index_of[i]);
        table.cstr(r.name);
        table.raw(resource_hash[i].data(), 20);
        table.num<std::int64_t>(static_cast<std::int64_t>(r.data.size()));
        table.num<std::uint8_t>(r.added ? 8 : 0);
        table.num<std::int32_t>(0);
        table.cstr(r.user_data);
        table.num<std::int32_t>(static_cast<std::int32_t>(r.bundles.size()));
        for (const auto& b : r.bundles) table.num<std::uint32_t>(frosty_hash(b));
        if (r.kind == ModResource::Kind::res) {
            table.num<std::uint32_t>(r.res_type);
            table.num<std::uint64_t>(r.res_rid);
            table.num<std::int32_t>(static_cast<std::int32_t>(r.res_meta.size()));
            table.bytes_of(r.res_meta);
        } else if (r.kind == ModResource::Kind::chunk) {
            table.num<std::uint32_t>(0);  // range start
            table.num<std::uint32_t>(0);  // range end
            table.num<std::uint32_t>(0);  // logical offset
            table.num<std::uint32_t>(static_cast<std::uint32_t>(r.data.size()));
            table.num<std::int32_t>(0);   // h32
            table.num<std::int32_t>(-1);  // first mip
            table.num<std::int32_t>(static_cast<std::int32_t>(r.superbundles.size()));
            for (const auto& s : r.superbundles) table.num<std::uint32_t>(frosty_hash(s));
        }
    }

    Out o;
    o.num<std::uint64_t>(0x01005954534F5246ull);
    o.num<std::uint32_t>(6);
    const std::size_t header = 8 + 4 + 8 + 4;
    o.num<std::int64_t>(static_cast<std::int64_t>(header + table.bytes.size()));
    o.num<std::int32_t>(static_cast<std::int32_t>(payloads.size()));
    o.bytes_of(table.bytes);
    std::int64_t offset = 0;
    for (const auto& p : payloads) {
        o.num<std::int64_t>(offset);
        o.num<std::int64_t>(static_cast<std::int64_t>(p.size()));
        offset += static_cast<std::int64_t>(p.size());
    }
    for (const auto& p : payloads) o.bytes_of(p);
    save(path, o.bytes);
}

} // namespace studio::native
